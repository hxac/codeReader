# Kernel 层与 MKI 框架

## 1. 本讲目标

本讲下沉到 ATB 的「最底层」——Kernel 层与 MKI 框架。学完后你应当能够：

- 说出 `src/kernels` 目录的分层（单算子 / 融合算子 / 通信算子）与各类算子的代码落点。
- 描述一个算子在 Kernel 层的「四件套」（kernel 计算、tiling 切分、operation 注册、CMake）分别是什么文件、各管什么。
- 理解 AscendC Kernel 的「三段式流水」CopyIn→Compute→CopyOut，以及 `TilingData`、`BlockDim`、`TilingKey` 三个核心概念。
- 读懂 MKI 框架的 `KernelBase` / `OperationBase` 与 `REG_KERNEL_BASE` / `REG_OPERATION` 注册机制，并明白它和上一讲（u3-l2）的 `OpsRunner→KernelGraph` 是如何衔接的。

本讲是后续 u6（自定义算子开发）的硬前置：只有先看懂一个现成算子是怎么在 Kernel 层「组装」起来的，才能谈自己新增一个。

## 2. 前置知识

阅读本讲前，你应当已经掌握（见前置讲义）：

- **两段式执行**：`Setup`（Host 侧校验 + 形状推导 + Tiling + 算 workspace）与 `Execute`（Device 侧异步下发），见 u1-l6。
- **调用链 Operation→Runner→KernelGraph→Kernel**：`OperationBase` 从不直接 launch kernel，而是经 `CreateRunner` 产出 Runner，Runner 内部维护一张 `KernelGraph`，Execute 时逐节点下发，见 u3-l2。
- **昇腾存储层级**（直觉版）：Device 上的全局内存（Global Memory，简称 GM，容量大但慢）与 AI Core 上的局部内存（Unified Buffer / UB，容量小但快）。Kernel 的核心工作就是把数据在 GM 与 UB 之间搬运，并在 UB 上做计算。

几个本讲会用到的术语：

- **AI Core**：昇腾 NPU 上真正执行计算的核，分 Vector 核（向量运算）与 Cube 核（矩阵乘）两类。
- **AscendC**：昇腾的算子开发语言/编程模型，用 `__aicore__` 标注在 AI Core 上运行的函数，提供 `DataCopy`、`Add`、`Cast` 等内置 API 与 `TQue`/`TPipe`/`GlobalTensor`/`LocalTensor` 等抽象。
- **Tiling（切分）**：在 Host 上决定数据怎么切分到多个核、每个核分几段处理，把结果写进 `TilingData` 传给 Device 端 kernel。
- **MKI**：ATB 依赖的「算子基础设施」第三方库（编译时拉取到 `3rdparty/mki`，即 `libmki`）。它提供 `KernelBase`、`OperationBase`、注册宏、`LaunchParam`、`KernelInfo`、`PlatformInfo` 等，是「ATB 框架」与「AscendC kernel 二进制」之间的中间调度层。

> ⚠️ 注意命名碰撞：ATB 框架层有一个 `atb::OperationBase`（u3-l1 讲过），MKI 层也有一个 `AsdOps::OperationBase`（本讲主角）。两者是不同命名空间下的不同类，本讲提到的 `OperationBase` 默认指 MKI 的 `AsdOps::OperationBase`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/starting_from_a_simple_operator.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md) | 官方「从零写一个 Add 算子」教程，是理解四件套的最佳导览 |
| [src/kernels/configs/build_config.json](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/configs/build_config.json) | 声明所有 kernel 可编译的目标芯片 |
| [src/kernels/include/asdops/params/params.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/include/asdops/params/params.h) | 汇总所有 Kernel 层算子参数 `OpParam::*` 的总头文件 |
| [src/kernels/kernels/concat/concat_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_operation.cpp) | 真实算子 Concat 的 MKI `OperationBase`：选 kernel + 形状推导 |
| [src/kernels/kernels/concat/concat_kernel/concat_kernel.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_kernel/concat_kernel.cpp) | 真实算子 Concat 的 MKI `KernelBase`：能力校验 + 触发 tiling |
| [src/kernels/kernels/elewise/quant/op_kernel/quant.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/op_kernel/quant.cpp) | 真实 AscendC kernel：三段式流水 + 从 GM 读 TilingData |
| [src/kernels/kernels/elewise/quant/quant_tiling/tiling_data.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/quant_tiling/tiling_data.h) | 真实 `TilingData` 结构定义 |
| [src/kernels/kernels/activation/gelu_forward/tiling/gelu_tiling.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/activation/gelu_forward/tiling/gelu_tiling.cpp) | 真实手写 tiling：取核数/UB、算切分、`SetBlockDim`/`SetTilingId` |
| [src/kernels/kernels/elewise/CMakeLists.txt](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/CMakeLists.txt) | `add_operation` / `add_kernel` 构建宏的真实用法 |

## 4. 核心概念与源码讲解

### 4.1 src/kernels 目录结构与算子分层

#### 4.1.1 概念说明

u1-l2 已经把 ATB 整体目录讲过一遍，这里只聚焦 `src/kernels/`。它是「真正跑在 NPU 上的计算代码」所在地，内部再按算子复杂度分三块：

```
src/kernels/
├── kernels/      # 单算子（element-wise、norm、concat 等，一个算子一个独立 kernel）
├── mixkernels/   # 融合算子（把多个计算融合成一个 kernel，如 kvcache、rope、laser_attention）
├── lcal/         # 通信/Cube 类算子（含 MatMul 的 TilingKey 打包逻辑）
├── configs/      # 编译配置（目标芯片、TBE tactic）
├── include/      # 对外头文件（asdops/params/*.h 参数定义）
└── tbe_adapter/  # TBE 适配器（供部分算子做 TBE 风格 tiling）
```

要点：`kernels/`（单算子，36 个）和 `mixkernels/`（融合算子，40 个）是两套并列的算子库，分别编进 `libasdops` 与 `libatb_mixops`。一个算子到底落在哪边，取决于它是不是「融合」——例如 `concat` 是单算子，`rms_norm_and_rope_and_reshape_and_cache` 是把多个操作融合成一次 kernel 启动的融合算子。

#### 4.1.2 核心流程

定位一个算子 Kernel 代码的流程：

1. 先到 `src/kernels/kernels/<算子名>/`（单算子）或 `src/kernels/mixkernels/<算子名>/`（融合算子）找四件套。
2. 它的参数结构在 `src/kernels/include/asdops/params/<算子名>.h`，并被汇总进 `params.h`。
3. 它支持哪些芯片，由各自 `CMakeLists.txt` 里的 `add_kernel(<算子> <芯片> ...)` 决定，全局可选芯片在 `build_config.json` 里声明。

#### 4.1.3 源码精读

目标芯片清单——目前支持四款昇腾芯片（[src/kernels/configs/build_config.json:2-7](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/configs/build_config.json#L2-L7)）：

```json
"targets": {
    "ascend310b": true,
    "ascend310p": true,
    "ascend910b": true,
    "ascend910": true
}
```

每个算子的参数类型都汇总在一个聚合类里，方便框架用「参数类型」做派发（[src/kernels/include/asdops/params/params.h:44-74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/include/asdops/params/params.h#L44-L74)）。`AllParams` 把 `Activation`、`Concat`、`MatMul`、`Elewise` 等近 30 个 `OpParam` 结构体并列放在一起——新增算子时，第一步就是把新参数头文件加进这个总头（见开发文档 L75-79）。每个 `OpParam` 都是带默认值、带 `operator==` 的 POD，例如 [src/kernels/include/asdops/params/concat.h:8-15](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/include/asdops/params/concat.h#L8-L15) 的 `struct Concat { int concatDim = 0; ... }`。

#### 4.1.4 代码实践

1. **目标**：建立「算子名 → 代码目录」的肌肉记忆。
2. **步骤**：在仓库里执行目录列举，确认 `kernels/` 与 `mixkernels/` 各有哪些算子。
3. **观察**：你会发现同名概念在两边都有，例如 `kvcache` 在 `mixkernels/`、`concat` 在 `kernels/`。
4. **预期结果**：能说出「我要看 RoPE 融合算子，应进 `mixkernels/rope`；看纯 concat，应进 `kernels/concat`」。

```bash
# 列出单算子与融合算子目录
ls src/kernels/kernels/      # 36 个单算子
ls src/kernels/mixkernels/   # 40 个融合算子
```

#### 4.1.5 小练习与答案

- **练习**：`rms_norm_and_rope_and_reshape_and_cache` 为什么放在 `mixkernels/` 而不是 `kernels/`？
- **答案**：因为它把 RMSNorm、RoPE、Reshape、Cache 写入四个计算**融合进同一个 kernel**，一次启动完成，属于融合算子；`kernels/` 里的算子是一个独立操作对应一个 kernel，不做跨操作融合。

---

### 4.2 Kernel「四件套」：kernel 计算 / tiling 切分 / operation 注册 / CMake

#### 4.2.1 概念说明

所谓「四件套」，是 u1-l2 提到的：一个算子在 Kernel 层通常由四类文件协作完成。我们以官方教程里的 `addcustom`（两向量相加）为例，它目录最规整（见 [docs/starting_from_a_simple_operator.md:18-33](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L18-L33)）：

| 第几件 | 文件 | 职责 | 运行位置 |
| --- | --- | --- | --- |
| ① kernel 计算 | `op_kernel/addcustom.cpp` | AscendC kernel：搬数据、算、写回（三段式流水） | Device（AI Core） |
| ② tiling 切分 | `tiling/addcustom_tiling.cpp/.h` + `tiling_data.h` | 决定切分策略，填充 `TilingData`，设置 `BlockDim` | Host（CPU） |
| ③ operation 注册 | `addcustom_operation.cpp` + `addcustom_kernel.cpp` | MKI 的 Operation 选最优 kernel、Kernel 触发 tiling，并用宏注册 | Host（CPU） |
| ④ CMake | `CMakeLists.txt` | `add_operation` 注册 Operation 源码、`add_kernel` 把 AscendC 源码编成二进制 | 构建期 |

记忆口诀：**「算（kernel）→ 切（tiling）→ 注册（operation）→ 编（CMake）」**。

> 真实仓库里目录命名会有微调：`concat` 把 kernel 注册放在 `concat_kernel/concat_kernel.cpp`、tiling 放在 `tiling/`；`elewise/quant` 把 AscendC kernel 放在 `op_kernel/quant.cpp`、tiling 放在 `quant_tiling/`。但「四类职责」始终存在。

#### 4.2.2 核心流程

四件套在一次 `Execute` 中的协作顺序（Host 段在 `Setup`/Tiling 阶段完成，Device 段在 `Execute` 阶段完成）：

```
Setup 阶段（Host）：
  MKI Operation.GetBestKernel  ──按 dtype/param 选一个 Kernel 子类──▶ KernelBase
  KernelBase.InitImpl          ──调用──▶ tiling 函数
  tiling 函数                  ──填充──▶ TilingData + 设 BlockDim/TilingId

Execute 阶段（Device）：
  框架把 TilingData 拷到 GM，启动 kernel 二进制
  AscendC kernel               ──从 GM 读 TilingData，按切分跑三段式流水──▶ 写回结果
```

注意：`TilingData` 是 Host 与 Device 之间唯一的「参数信使」——Host 把切分结果写进去，Device 端 kernel 第一步就是把它从 GM 读到 UB。

#### 4.2.3 源码精读

**第④件 CMake**：把 Operation 的三个源文件注册给框架，并声明 kernel 二进制（[src/kernels/kernels/concat/CMakeLists.txt:11-17](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/CMakeLists.txt#L11-L17)）——`add_operation(ConcatOperation "${concat_srcs}")` 把 Operation/Kernel/tiling 的 `.cpp` 编进 `libasdops`。

`add_kernel` 宏则负责把 AscendC 源码编成可在指定芯片上运行的二进制，签名是 `add_kernel(<算子名> <芯片> <核类型> <源码> <Kernel类名>)`。看 quant 的真实用法（[src/kernels/kernels/elewise/CMakeLists.txt:58-60](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/CMakeLists.txt#L58-L60)）：`add_kernel(quant ascend910b vector quant/op_kernel/quant.cpp QuantF16Kernel)`——意为「把 `quant.cpp` 按 910b 芯片的 vector 核编成二进制，关联到 `QuantF16Kernel`」。同一个 AscendC 源码可以为多款芯片各注册一次。

**第②件 tiling 的数据契约**：`TilingData` 是 Host/Device 共享的 POD。quant 的真实定义（[src/kernels/kernels/elewise/quant/quant_tiling/tiling_data.h:17-27](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/quant_tiling/tiling_data.h#L17-L27)）：

```cpp
struct QuantF16TilingData {
    uint32_t numCore{0};            // 激活多少个核
    uint32_t numLastDim{0};         // 最后一维大小
    uint32_t numFirstDim{0};        // 首维大小
    uint32_t nlFirstdimPerCore{0};  // 非末核每核分到的行数
    uint32_t lFirstdimPerCore{0};   // 末核分到的行数
    uint32_t firstDimPerTimes{0};   // 每次搬入多少行
    uint32_t inputScale{0};
    uint32_t inputOffset{0};
    float quantMin{-128};
};
```

这正是「Host 写、Device 读」的切分参数表。

#### 4.2.4 代码实践

1. **目标**：把四件套对号入座。
2. **步骤**：打开 `src/kernels/kernels/concat/`，找出 ① 计算、② tiling、③ 注册、④ CMake 四类文件。
3. **观察**：concat 没有手写 AscendC kernel（① 用 TBE 适配器代替，见 4.3.4），但有完整的 ②③④。
4. **预期结果**：列出 `concat_kernel/concat_kernel.cpp`（③的 Kernel 部分）、`concat_operation.cpp`（③的 Operation 部分）、`tiling/concat_tiling.cpp/.h`（②）、`CMakeLists.txt`（④）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 AscendC kernel 源码（`op_kernel/*.cpp`）不直接编进 `libasdops`，而要走 `add_kernel` 单独编成二进制？
- **答案**：因为 AscendC kernel 用 `__aicore__` 标注、是跑在 AI Core 上的设备码，需要专门的编译器（CCE）针对每款芯片编译成二进制；而 Operation/tiling 是跑在 Host（CPU）上的普通 C++，用 `add_operation` 进 `libasdops` 即可。两者工具链不同。
- **练习 2**：四件套中，哪一件是「Host 与 Device 之间的信使」？
- **答案**：② tiling 切分里的 `TilingData` 结构——Host 填、Device 读。

---

### 4.3 AscendC Kernel：三段式流水与 TilingData / BlockDim / TilingKey

#### 4.3.1 概念说明

AscendC kernel 是真正跑在 AI Core 上的代码。它的标准写法是**三段式流水线**：每个数据块依次经过 `CopyIn`（GM→UB 搬入）、`Compute`（UB 上计算）、`CopyOut`（UB→GM 写回）。借助 `TQue` 队列与双缓冲（`BUFFER_NUM`），搬入/计算/搬出三段可以重叠流水，掩盖访存延迟。

三个必须吃透的概念：

- **TilingData**：见 4.2.3，Host 算好的切分参数，kernel 启动后第一件事是从 GM 把它读进 UB。
- **BlockDim**：本次 kernel 启动**激活多少个核**（并行度）。Host 在 tiling 阶段用 `kernelInfo.SetBlockDim(n)` 设定；Device 端用 `GetBlockNum()`/`GetBlockIdx()` 获知总核数与本核编号，从而切分数据。
- **TilingKey（TilingId）**：一个「分支选择码」。同一份 kernel 二进制可能要处理多种情形（如不同 dtype、是否转置），Host 用它告诉 Device 走哪个分支。ATB 单算子层用 MKI 的 `kernelInfo.SetTilingId(...)`；`lcal` 的 Cube 算子则用「位域打包」的 `TilingKey`，把多个布尔开关压成一个整数。

#### 4.3.2 核心流程

一个 AscendC kernel 的执行骨架（伪代码）：

```
extern "C" __aicore__ void kernel(GM_ADDR 输入..., GM_ADDR tiling):
    TilingData t
    从 GM 把 tiling 拷到 UB，解析进 t          # 读切分参数
    op.Init(输入..., &t)                        # 建 GlobalTensor、分配 UB 队列
    op.Process()                                # 循环：每段数据 CopyIn→Compute→CopyOut
```

切分时的典型计算（Host 端）：

\[ \text{blockLength} = \left\lceil \frac{\text{totalLength}}{\text{coreNum}} \right\rceil \quad(\text{每个核分到的元素数}) \]

\[ \text{tileNum} = \left\lceil \frac{\text{blockLength}}{\text{maxPerUb}} \right\rceil \quad(\text{每个核内分几段搬入}) \]

末核通常分到的数据 (`lFirstdimPerCore`) 与其它核 (`nlFirstdimPerCore`) 不同，因此 `TilingData` 里这两个字段要分开存。

#### 4.3.3 源码精读

**quant 的真实三段式流水**（[src/kernels/kernels/elewise/quant/op_kernel/quant.cpp:68-98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/op_kernel/quant.cpp#L68-L98)）——`Process()` 循环里依次调 `CopyIn`→`Compute`→`CopyOut`，中间用 `SetFlag/WaitFlag` 做流水同步（MTE2/V/MTE2 事件）：

```cpp
__aicore__ inline void Process() {
    uint32_t move_cnt = CEIL_DIV(row_work, row_step);
    for (uint64_t i = 0; i < move_cnt; ++i) {
        // ... 末段与非末段分别处理
        CopyIn(i, ...);   // GM -> UB
        Compute(...);     // UB 上 Muls -> Adds -> Cast(float16->int8)
        CopyOut(i, ...);  // UB -> GM
    }
}
```

`Compute` 在 UB 上做实际计算（[src/kernels/kernels/elewise/quant/op_kernel/quant.cpp:110-128](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/op_kernel/quant.cpp#L110-L128)）：逐行 `Muls`（乘 scale）、`Adds`（加 offset）、`CastFromF16ToI8`（转 int8）。

kernel 入口（[src/kernels/kernels/elewise/quant/op_kernel/quant.cpp:189-198](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/op_kernel/quant.cpp#L189-L198)）展示了「先读 TilingData 再 Init 再 Process」的标准三步：`extern "C" __global__ __aicore__ void quant(GM_ADDR x, GM_ADDR z, GM_ADDR tiling)`。

> 想看最简洁的三段式，仍推荐官方教程的 addcustom（[docs/starting_from_a_simple_operator.md:296-347](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L296-L347)）：`Process`/`CopyIn`/`Compute`/`CopyOut` 四个函数一气呵成，没有量化那种逐行循环与事件同步，适合建立第一印象。

**手写 tiling 的真实样例**（gelu，[src/kernels/kernels/activation/gelu_forward/tiling/gelu_tiling.cpp:80-97](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/activation/gelu_forward/tiling/gelu_tiling.cpp#L80-L97)）。先取硬件信息（[L38-40](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/activation/gelu_forward/tiling/gelu_tiling.cpp#L38-L40)）`PlatformInfo::Instance().GetCoreNum(CORE_TYPE_VECTOR)` 与 `GetUbSize()`，再算切分，最后：

```cpp
kernelInfo.SetBlockDim(blockDim);        // 设 BlockDim：激活多少核
kernelInfo.SetTilingId(dataType);        // 设 TilingId：用 dtype 当分支码
```

**TilingKey 的位域打包样例**（lcal 的 MatMul，[src/kernels/lcal/src/tiling/tiling_func.cpp:75-83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/lcal/src/tiling/tiling_func.cpp#L75-L83)）——把 `swizzlDirect/transA/transB/isInt8/withBias/splitK` 六个开关逐位左移拼成一个 `uint32_t tilingKey`，Device 端据此走不同的矩阵乘分支。这就是 Cube 算子比 Vector 算子更复杂的「TilingKey」用法。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：理解 TilingData 如何从 Host 传到 Device。
2. **步骤**：对比两处「读 TilingData」代码——quant 的 `InitTilingData`（[src/kernels/kernels/elewise/quant/op_kernel/quant.cpp:159-187](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/op_kernel/quant.cpp#L159-L187)）与 addcustom 教程的 `InitTilingData`（[docs/starting_from_a_simple_operator.md:362-366](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L362-L366)）。
3. **观察**：quant 用 `CopyGmTilingToUb` 把 tiling 先搬到 UB 再逐字段读（带 `__CCE_AICORE__` 与 kernel 测试两种分支）；addcustom 直接按 `uint32_t` 偏移读。
4. **预期结果**：能说清「TilingData 的字段顺序与字节偏移必须在 Host 端 tiling 与 Device 端 InitTilingData 之间严格对齐，否则会读到错位的切分参数」。
5. 运行结果：待本地验证（需昇腾环境编译 kernel）。

#### 4.3.5 小练习与答案

- **练习 1**：`BlockDim` 与 `tileNum` 有什么区别？
- **答案**：`BlockDim` 是**核间**并行度（激活几个 AI Core），由 `SetBlockDim` 设；`tileNum` 是**核内**一个核要把自己的数据分几段搬入计算（受 UB 容量限制）。前者是核外切分，后者是核内切分。
- **练习 2**：为什么末核分到的行数要单独存成 `lFirstdimPerCore`？
- **答案**：总数据 often 不能被核数整除，末核分到的余数与其它核不同；单独存避免 Device 端再做取余判断，也便于给末核不同的 tail 处理。

---

### 4.4 MKI 注册框架：KernelBase / OperationBase / REG 宏

#### 4.4.1 概念说明

四件套里的「③ operation 注册」依赖 MKI 提供的两个基类和两个注册宏。这是本讲的硬核：

- **`KernelBase`**：一个 kernel 变体的 Host 侧包装。核心钩子：
  - `CanSupport(launchParam)`——这个 kernel 能否处理当前 param/dtype/形状？
  - `GetTilingSize()`——返回 `sizeof(TilingData)`，告诉框架要分配多大 tiling 缓冲。
  - `InitImpl(launchParam)`——执行 tiling，填充 `kernelInfo_`（含 BlockDim/TilingId）。
  - 用 `REG_KERNEL_BASE(类名)` 注册，注册名即 kernel 名。
- **`OperationBase`**（MKI 版）：一个算子的调度入口。核心钩子：
  - `GetBestKernel(launchParam)`——按 dtype/param 选最优 kernel，返回 `GetKernelByName("注册名")`。
  - `GetInputNum/GetOutputNum`——张量个数。
  - `InferShapeImpl`——输出形状推导。
  - 用 `REG_OPERATION(类名)` 注册，注册名是 u3-l2 里 `KernelGraphNode.opDesc` 字符串（如 `"AddcustomOperation"`、`"ConcatOperation"`）。

`LaunchParam` 是 MKI 层的「厚集装箱」，相当于 Runner 层的 `RunnerVariantPack`：它装着 `OpParam`（以 `Any` 类型擦除）、输入输出 `Tensor`、tiling 缓冲等，是 Host 侧 tiling 与校验函数的统一入参。

#### 4.4.2 核心流程

把 u3-l2 的调用链接到底，完整的「框架→MKI→二进制」链路：

```
ATB OpsRunner.SetupKernelGraph:
    node.opDesc = {0, "ConcatOperation", opParam}     # 字符串名 = MKI 注册名
ATB OpsRunner.Execute:
    node.impl.Run(stream)
        └─▶ 按 "ConcatOperation" 在 MKI 注册表查到 AsdOps::ConcatOperation
            └─▶ GetBestKernel: 按 dtype 选 "ConcatF16Input2Kernel" / "ConcatF32Input2Kernel"
                └─▶ KernelBase.InitImpl: 跑 tiling，填 BlockDim
                    └─▶ 框架拷 TilingData 到 GM，启动 kernel 二进制
```

关键洞察：**MKI 注册名是 ATB Runner 与 MKI Operation 之间的耦合点**。Runner 在 `opDesc` 里写字符串，MKI 用同名 `REG_OPERATION` 兜住。同理 Operation 内部 `GetKernelByName` 的字符串，必须与 `REG_KERNEL_BASE` 的注册名一致。

#### 4.4.3 源码精读

**Operation 侧（concat）**——`GetBestKernel` 按输出 dtype 分派到两个 kernel（[src/kernels/kernels/concat/concat_operation.cpp:21-30](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_operation.cpp#L21-L30)）：

```cpp
Kernel *GetBestKernel(const LaunchParam &launchParam) const override {
    MKI_CHECK(IsConsistent(launchParam), "Fail to check consistent", return nullptr);
    auto dtype = launchParam.GetOutTensor(0).desc.dtype;
    if (dtype == TENSOR_DTYPE_FLOAT) {
        return GetKernelByName("ConcatF32Input2Kernel");
    } else {
        return GetKernelByName("ConcatF16Input2Kernel");
    }
}
```

`InferShapeImpl` 推导「在 concatDim 维度上相加」的输出形状（[src/kernels/kernels/concat/concat_operation.cpp:39-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_operation.cpp#L39-L68)）：`dims.at(concatDim) = dims.at(concatDim) + dims1.at(concatDim)`。最后 [L70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_operation.cpp#L70) `REG_OPERATION(ConcatOperation);` 一行完成注册。

**Kernel 侧（concat）**——`ConcatKernel` 继承 `KernelBase`，`CanSupport` 校验 param 类型/张量数/dtype/concatDim 合法性（[src/kernels/kernels/concat/concat_kernel/concat_kernel.cpp:25-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_kernel/concat_kernel.cpp#L25-L44)），`InitImpl` 转调 tiling 函数（[L46-49](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_kernel/concat_kernel.cpp#L46-L49)）。然后派生两个空壳子类仅用于「注册名不同」（[L52-68](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/concat/concat_kernel/concat_kernel.cpp#L52-L68)）：

```cpp
class ConcatF16Input2Kernel : public ConcatKernel { ... };
REG_KERNEL_BASE(ConcatF16Input2Kernel);   // 名字必须与 GetKernelByName 的一致
class ConcatF32Input2Kernel : public ConcatKernel { ... };
REG_KERNEL_BASE(ConcatF32Input2Kernel);
```

**Kernel 侧（quant）**——更能体现 `GetTilingSize` 的作用：`QuantF16Kernel` 重写 `GetTilingSize` 返回 `sizeof(QuantF16TilingData)`（[src/kernels/kernels/elewise/quant/quant_kernel.cpp:65-71](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/quant_kernel.cpp#L65-L71)），`InitImpl` 调 `QuantF16Tiling`（[L41-44](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/kernels/kernels/elewise/quant/quant_kernel.cpp#L41-L44)），末尾 `REG_KERNEL_BASE(QuantF16Kernel)`。

> 官方教程对这套基类有最干净的对照实现，建议对照阅读：`AddcustomKernel`（[docs/starting_from_a_simple_operator.md:394-425](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L394-L425)）与 `AddcustomOperation`（[docs/starting_from_a_simple_operator.md:444-495](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L444-L495)）。

#### 4.4.4 代码实践

1. **目标**：验证「注册名 = 调用名」这条铁律。
2. **步骤**：在 concat 三处 grep 同一个名字 `ConcatF16Input2Kernel`：① `concat_kernel.cpp` 的 `REG_KERNEL_BASE`、② `concat_operation.cpp` 的 `GetKernelByName`、③ `CMakeLists.txt` 里 `add_kernel` 关联的 Kernel 类。
3. **需要观察的现象**：三处名字必须完全一致；若手滑改成 `ConcatF16Kernel`，框架在运行时会查不到 kernel 而返回空指针。
4. **预期结果**：能画出 `REG_KERNEL_BASE 名 ↔ GetKernelByName 名 ↔ add_kernel 关联名` 三者必须一致的关系图。
5. 运行结果：待本地验证（运行时改名会触发「kernel not found」类错误）。

```bash
# 验证注册名一致性的三条 grep
grep -rn "ConcatF16Input2Kernel\|ConcatF32Input2Kernel" src/kernels/kernels/concat/
```

#### 4.4.5 小练习与答案

- **练习 1**：一个 `OperationBase` 为什么可能对应多个 `KernelBase`？
- **答案**：因为同一算子在不同 dtype/形状/芯片下需要不同的 kernel 实现或切分策略。`GetBestKernel` 的职责就是按运行时信息在多个注册 kernel 里挑一个，例如 concat 按 float/float16 选 `ConcatF32Input2Kernel`/`ConcatF16Input2Kernel`。
- **练习 2**：`REG_OPERATION(ConcatOperation)` 注册的名字，被谁用？
- **答案**：被上游 ATB `OpsRunner` 用——它在 `SetupKernelGraph` 里把节点 `opDesc` 的字符串设为 `"ConcatOperation"`（见 u3-l2），Execute 时按这个名字从 MKI 注册表查到 `ConcatOperation` 实例。

---

## 5. 综合实践

**任务**：仿照官方 addcustom 教程，为一个假想的 `Xorcustom`（两 int32 向量按位异或）算子，列出 Kernel 层四件套的**完整文件清单与各自职责**，并指出三处必须同名的「注册名」。

完成步骤（源码阅读 + 设计型，无需真实编译）：

1. 重读开发文档的「New Files」段落（[docs/starting_from_a_simple_operator.md:18-71](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/starting_from_a_simple_operator.md#L18-L71)），理解每个文件用途。
2. 参考真实算子 concat 的最小实现（`concat_operation.cpp` + `concat_kernel.cpp` + `tiling/` + `CMakeLists.txt`）。
3. 产出一张表，包含：文件路径、属于第几件（①②③④）、运行位置（Host/Device）、关键函数/结构。
4. 用一段伪代码写出 `XorcustomOperation::GetBestKernel` 与 `XorcustomKernel::InitImpl` 的骨架，并明确标注三处必须同名的字符串（`REG_OPERATION`、`opDesc`、`REG_KERNEL_BASE`/`GetKernelByName`/`add_kernel`）。

**自检要点**：

- `TilingData` 至少要有 `totalLength` 和 `tileNum`，且字段顺序在 tiling 函数与 kernel 的 `InitTilingData` 之间一致。
- `BlockDim` 不超过 `PlatformInfo::GetCoreNum(CORE_TYPE_VECTOR)`。
- `CMakeLists.txt` 里 `add_operation` 收录 Operation/Kernel/tiling 源码，`add_kernel` 至少为 `ascend910b vector` 注册一份。

> 本任务只设计、不落盘。真正「动手写一个新算子并接入 ATB 框架」是 u6-l2/u6-l3 的内容，本讲只要求你把 Kernel 层的地基看懂。

## 6. 本讲小结

- `src/kernels` 分三块：`kernels/`（单算子，36 个）、`mixkernels/`（融合算子，40 个）、`lcal/`（通信/Cube 算子），分别对应 `libasdops`、`libatb_mixops` 与 Cube 库。
- 一个算子在 Kernel 层由**四件套**组成：① AscendC kernel 计算（Device）、② tiling 切分（Host）、③ MKI Operation/Kernel 注册（Host）、④ CMake 构建。
- `TilingData` 是 Host 与 Device 之间唯一的参数信使；`BlockDim` 决定核间并行度，`TilingKey/TilingId` 是分支选择码（单算子用 `SetTilingId`，Cube 算子用位域打包）。
- AscendC kernel 标准写法是 **CopyIn→Compute→CopyOut 三段式流水**，配合 `TQue` 双缓冲掩盖访存延迟。
- MKI 框架用 `KernelBase`（管 tiling 与能力校验，`REG_KERNEL_BASE` 注册）与 `OperationBase`（管选 kernel 与形状推导，`REG_OPERATION` 注册）把 ATB Runner 与 kernel 二进制衔接起来。
- **注册名是耦合点**：`REG_OPERATION` 名 = Runner `opDesc` 字符串；`REG_KERNEL_BASE` 名 = `GetKernelByName` 名 = `add_kernel` 关联的 Kernel 类名，三处必须完全一致。

## 7. 下一步学习建议

- 想看「ATB 框架如何把字符串 opDesc 路由到 MKI Operation」，回看 u3-l2 的 `OpsRunner` 与 `KernelGraphNode.impl`（`AtbKernelMethod`）部分，把本讲的「注册名」与那讲的「节点下发」对上。
- 准备进入 u6 单元「自定义算子开发」：u6-l2 会以 `customize_blockcopy` 为例手把手写 AscendC kernel，u6-l3 会把 kernel 接进 ATB 的 Operation/Runner，本讲的四件套与 MKI 注册是那里的直接前置。
- 若想深入 AscendC 编程模型（`TQue`/`TPipe`/`GlobalTensor`/`LocalTensor`/事件同步），建议在昇腾官方「Ascend C 算子开发指南」补充阅读，本讲只覆盖了读 ATB 源码所需的最小集。
- 进阶可读 `src/kernels/mixkernels/` 下的融合算子（如 `rope`、`kvcache`），观察融合算子的四件套与单算子有何不同（通常 tiling 更复杂、kernel 更长）。
