# Kernel 层 BasicMatmul 与 SPMD 循环

## 1. 本讲目标

在上一讲（u2-l3）中，我们站在 Host 侧看到 `DeviceGemm` 把 `Arguments` 转成 `Params`、再用 `<<<blockDim>>>` 把算子派发到 NPU。派发之后就进入设备侧了——本讲就拆开「被派发的那段设备代码」，也就是 Kernel 层的 `BasicMatmul`。

学完本讲你应当能够：

- 说清 `BasicMatmul` 内部 `Params` 与 `Arguments` 两个结构体的分工，以及 `ToUnderlyingArguments` 是如何把「用户给的尺寸 + 指针」补全成「带布局的内核参数」的。
- 看懂设备侧那段 SPMD 主循环：`for (loopIdx = GetBlockIdx(); loopIdx < coreLoops; loopIdx += GetBlockNum())` 是怎样用「步长等于核数」的方式把 C 矩阵的基本块分摊到各个 AICore 的。
- 解释 `BlockScheduler` 的三个关键接口 `GetCoreLoops / GetBlockCoord / GetActualBlockShape` 各自负责什么，并能把一个基本块从「块坐标」一路追到「GM 偏移」再到「`blockMmad` 调用」。

本讲是 Block 层（U4）和 Tile 层（U5）的入口：`BasicMatmul` 把单个 C 基本块的计算委托给 `blockMmad`，而 `blockMmad` 内部才是真正的 K 维主循环和硬件搬运。读懂本讲，你就能站在「多核如何分活」的视角俯瞰整个 Kernel。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前面几讲），这里只做最简回顾：

- **五层抽象**（u1-l1）：Device → Kernel → Block → Tile → Basic。本讲处于 **Kernel 层**，向上接 Device 层的派发，向下调 Block 层的 `blockMmad`。
- **昇腾存储层级与 SPMD 模型**（u1-l2）：所有 AICore 跑同一份 kernel，靠 `GetBlockIdx()`（本核编号）和 `GetBlockNum()`（总核数）区分各自要处理的数据。矩阵数据放在全局内存 GM 上。
- **Host 侧组装链**（u2-l2 / u2-l3）：`MatmulKernel = BasicMatmul<BlockMmad, BlockEpilogue, BlockScheduler>`，再用 `DeviceGemm` 包装。Device 层把用户 `Arguments` 转成 Kernel `Params` 后派发。

两个本讲会反复用到的小概念：

- **GemmCoord / MatrixCoord**：三维 `(m, n, k)` 与二维 `(row, column)` 的坐标包装类，提供 `.m() / .n() / .k()` 与 `.row() / .column()` 等具名访问（见 [include/catlass/gemm_coord.hpp:66-156](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm_coord.hpp#L66-L156)、[include/catlass/matrix_coord.hpp:34-112](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/matrix_coord.hpp#L34-L112)）。
- **Layout**：描述一个矩阵在 GM 中「逻辑坐标 → 线性偏移」的映射，如 `RowMajor` 的偏移是 `row*stride + col`（见 [include/catlass/layout/matrix.hpp:79-83](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L79-L83)）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/catlass/gemm/kernel/basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp) | 本讲主角。定义 Kernel 层 `BasicMatmul`，包含 `Params/Arguments`、`ToUnderlyingArguments`、以及设备侧 `operator()<AIC>` 的 SPMD 主循环。 |
| [include/catlass/gemm/block/block_swizzle.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp) | `BlockScheduler` 的具体实现 `GemmIdentityBlockSwizzle`，提供 `GetCoreLoops/GetBlockCoord/GetActualBlockShape`。 |
| [include/catlass/arch/resource.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/resource.hpp) | `Arch::Resource<ArchTag>`，单核内各级存储（L1/L0A/L0B/L0C/UB）缓冲的集合，构造 `blockMmad` 时传入。 |
| [include/catlass/layout/matrix.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp) | `RowMajor`/`ColumnMajor` 的 `MakeLayout` 与 `GetOffset`，理解 GM 偏移如何算出来。 |
| [docs/zh/3_API/gemm_api.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md) | 官方对「三层嵌套循环」伪代码与 Kernel 层职责的说明。 |

## 4. 核心概念与源码讲解

先把整体形状画出来。Kernel 层的 `BasicMatmul` 是一个**无状态**的设备侧类（无成员变量，状态由调用方通过 `Params` 传入）。它做三件事：

1. 在 Host 侧，用 `ToUnderlyingArguments` 把用户的 `Arguments` 补全成 `Params`。
2. 在设备侧，用 `BlockScheduler` 把 C 矩阵切成一堆基本块（block），并算出总块数 `coreLoops`。
3. 用 SPMD 步长循环让每个 AICore 认领其中一部分块，对每块算出 GM 偏移，再调用 `blockMmad` 完成计算。

下面按三个最小模块依次拆解。

### 4.1 Params 与 Arguments：用户视图与内核视图的分离

#### 4.1.1 概念说明

你也许会奇怪：为什么要有 `Arguments` 和 `Params` 两个看起来差不多的结构体？这是 CATLASS 全程贯彻的「两层视图」设计（u2-l3 已为 Device 层讲过，Kernel 层是同一思想的延续）：

- **`Arguments`（用户视图）**：用户在 Host 侧只需要告诉算子「矩阵多大」和「三个矩阵的 GM 指针」。用户**不需要**也不应该关心 A/B/C 的具体排布（行主序还是列主序）——那是底层细节。所以 `Arguments` 只装 `problemShape` 和三个指针。
- **`Params`（内核视图）**：设备侧算 GM 偏移时必须知道每个元素的排布方式，否则没法把「第 m 块、第 n 块」翻译成字节地址。所以 `Params` 在指针之外，额外携带了 `LayoutA / LayoutB / LayoutC`。

把 Layout 放在 `Params` 而不是 `Arguments`，是为了让用户接口保持简单；而 Layout 是由**元素类型 + 编译期模板参数**决定的，完全可以由 `ToUnderlyingArguments` 自动构造，不需要用户操心。

#### 4.1.2 核心流程

```
Host 侧：
  Arguments{problemShape, ptrA, ptrB, ptrC}
        │  ToUnderlyingArguments(args, workspace)
        ▼
  Params{problemShape, ptrA, layoutA, ptrB, layoutB, ptrC, layoutC}
        │  被 <<<blockDim>>> 派发，原样传给每个 AICore
        ▼
设备侧 operator()<AIC>(params) 直接使用 params.layoutA.GetOffset(...)
```

关键点：`ToUnderlyingArguments` 在 Host 上运行，它调用每个 Layout 的 `MakeLayout<Element>(行, 列)` 来「物化」一个布局对象。对 `RowMajor` 而言，这一步本质上是记下 `shape = (rows, cols)`、`stride = (cols, 1)`。

#### 4.1.3 源码精读

先看两个结构体的定义。[include/catlass/gemm/kernel/basic_matmul.hpp:40-74](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L40-L74)：

```cpp
struct Params {
    GemmCoord problemShape;
    GM_ADDR ptrA;  LayoutA layoutA;
    GM_ADDR ptrB;  LayoutB layoutB;
    GM_ADDR ptrC;  LayoutC layoutC;
    // 构造函数略
};

struct Arguments {
    GemmCoord problemShape;
    GM_ADDR ptrA;
    GM_ADDR ptrB;
    GM_ADDR ptrC;
};
```

可以看到 `Params` 相比 `Arguments` 多出的就是三个 `layoutXxx`。`GM_ADDR` 是 ACL 提供的设备 GM 地址类型。

`ToUnderlyingArguments` 负责「补 Layout」，见 [include/catlass/gemm/kernel/basic_matmul.hpp:86-93](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L86-L93)：

```cpp
static Params ToUnderlyingArguments(const Arguments& args, uint8_t* workspace)
{
    LayoutA layoutA = LayoutA::template MakeLayout<ElementA>(args.problemShape.m(), args.problemShape.k());
    LayoutB layoutB = LayoutB::template MakeLayout<ElementB>(args.problemShape.k(), args.problemShape.n());
    LayoutC layoutC = LayoutC::template MakeLayout<ElementC>(args.problemShape.m(), args.problemShape.n());
    Params params{args.problemShape, args.ptrA, layoutA, args.ptrB, layoutB, args.ptrC, layoutC};
    return params;
}
```

注意三个 Layout 的尺寸对应矩阵乘的约定 \( C_{M \times N} = A_{M \times K} \cdot B_{K \times N} \)：

- A 是 `(M, K)`，所以传 `(problemShape.m(), problemShape.k())`
- B 是 `(K, N)`，所以传 `(problemShape.k(), problemShape.n())`
- C 是 `(M, N)`，所以传 `(problemShape.m(), problemShape.n())`

`MakeLayout` 是个静态模板函数，以 `RowMajor` 为例（[include/catlass/layout/matrix.hpp:59-68](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L59-L68)）：它记录下矩阵的行列数，并把行步长设为列数（`stride=(cols, 1)`）。`MakeLayout<Element>` 还会按元素类型和架构做必要的对齐修正（例如 `3510` 架构对 4bit 类型的 `RoundUp<2>`），但核心仍是确定 `shape/stride`。

> 拓展：`BasicMatmul` 另外两个静态方法 `CanImplement`（[L76-L79](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L76-L79)，恒返回 `true`）和 `GetWorkspaceSize`（[L81-L84](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L81-L84)，恒返回 `0`）是给 Device 层转发的——这与 u2-l3 讲的「BasicMatmul 不需要 workspace、SplitkMatmul 才需要」完全对应。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `ToUnderlyingArguments` 构造出的 Layout 尺寸正确。

**操作步骤**（源码阅读型）：

1. 打开 [basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp) 的 `ToUnderlyingArguments`。
2. 假设用户输入 `problemShape = (M=256, N=512, K=1024)`，在纸上写出三个 `MakeLayout` 的入参。
3. 对照 [RowMajor::MakeLayout](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L59-L68) 和 [RowMajor::GetOffset](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L79-L83)，求 `layoutA` 的 `shape_` 与 `stride_`。

**预期结果**：

- `MakeLayout<ElementA>(256, 1024)` → A 的 `shape=(256,1024)`、`stride=(1024,1)`。
- `MakeLayout<ElementB>(1024, 512)` → B 的 `shape=(1024,512)`。
- `MakeLayout<ElementC>(256, 512)` → C 的 `shape=(256,512)`、`stride=(512,1)`。

**需要观察的现象**：A、B、C 的 Layout 尺寸分别严格等于 `(M,K)`、`(K,N)`、`(M,N)`，三者刚好能拼出矩阵乘的维度契约。这一步是后续所有 GM 偏移计算的地基。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Arguments` 里不放 `LayoutA`，而要等到 `ToUnderlyingArguments` 才构造？
**答案**：为了让用户接口保持最小化——用户只需提供尺寸和指针。Layout 由编译期已知的 `LayoutA/ElementA` 类型推导得出，属于实现细节，放在内核视图 `Params` 里即可。

**练习 2**：若把 `problemShape` 的 M 与 N 写反了（误传成 `(N, M, K)`），下游哪个环节会出错？
**答案**：`ToUnderlyingArguments` 会用错误的尺寸构造 Layout，导致 `GetOffset` 算出的 GM 偏移越界、读到/写出错误的内存。这也是为什么 `problemShape` 的语义被严格约定为 `(M, N, K)`。

---

### 4.2 SPMD 分核循环：用步长等于核数把块分给各核

#### 4.2.1 概念说明

昇腾 NPU 一颗芯片上有数十个 AICore，它们**运行同一份 kernel 代码**（SPMD：Single Program, Multiple Data）。既然代码相同，各核就必须有办法「知道自己是几号、总共多少核、该认领哪些活」。这就是 `AscendC::GetBlockIdx()`（本核编号，从 0 起）和 `AscendC::GetBlockNum()`（总核数）的用途。

`BasicMatmul` 采用一种极简而优雅的分活策略——**步长循环（strided loop）**：

```
for (loopIdx = GetBlockIdx(); loopIdx < coreLoops; loopIdx += GetBlockNum()) {
    处理第 loopIdx 个基本块;
}
```

- 起点设为本核编号 `GetBlockIdx()`：核 0 从第 0 块开始、核 1 从第 1 块开始……
- 步长设为总核数 `GetBlockNum()`：每个核每隔 `coreNum` 个块认领一块。

于是 4 个核、10 个块时：核 0 做 `0,4,8`，核 1 做 `1,5,9`，核 2 做 `2,6`，核 3 做 `3,7`。每个块恰好被一个核处理，无重复无遗漏。这种写法相比「显式两层 for 切 M/N」更紧凑，也天然把「块到核的映射」这件事交给 `GetBlockCoord` 去做。

#### 4.2.2 核心流程

`operator()` 是个模板，按核类型 `CORE_TYPE` 特化（[include/catlass/gemm/kernel/basic_matmul.hpp:100-105](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L100-L105)）。一颗 AI Core 内部又分 **AIC**（Cube，做矩阵乘）和 **AIV**（Vector，做后处理）两个子核。`BasicMatmul` 把真正的计算放在 `operator()<AIC>`，而 `operator()<AIV>` 留空（本例没有后处理）。整体流程：

```
operator()<AIC>(params):
  1. 构造 BlockScheduler，算出总块数 coreLoops
  2. 构造单核资源 resource 与 blockMmad
  3. 把 ptrA/ptrB/ptrC 包装成 GlobalTensor（设备侧的 GM 视图）
  4. SPMD 步长循环：
       for loopIdx = GetBlockIdx(); loopIdx < coreLoops; loopIdx += GetBlockNum():
           blockCoord      = scheduler.GetBlockCoord(loopIdx)       // 块号 (mIdx, nIdx)
           actualBlockShape= scheduler.GetActualBlockShape(blockCoord)// 触边裁剪后的真实尺寸
           offsetA/B/C     = blockCoord * L1TileShape              // 逻辑坐标
           gmOffsetA/B/C   = layoutX.GetOffset(offsetX)           // 物理字节偏移
           blockMmad(gmA[gmOffsetA], ..., gmC[gmOffsetC], actualBlockShape)  // 交给 Block 层
  5. PipeBarrier<PIPE_ALL>()  // 核内所有流水归位
```

#### 4.2.3 源码精读

设备侧 `operator()<AIC>` 的完整主体在 [include/catlass/gemm/kernel/basic_matmul.hpp:104-141](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L104-L141)。逐段看：

**① 构造调度器、资源、blockMmad**（[L107-L111](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L107-L111)）：

```cpp
BlockScheduler matmulBlockScheduler(params.problemShape, MakeCoord(L1TileShape::M, L1TileShape::N));
uint32_t coreLoops = matmulBlockScheduler.GetCoreLoops();

Arch::Resource<ArchTag> resource;
BlockMmad blockMmad(resource);
```

调度器用「问题尺寸 + L1Tile 的 (M,N)」初始化；`Resource`（[include/catlass/arch/resource.hpp:19-39](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/resource.hpp#L19-L39)）持有本核各级存储缓冲（L1/L0A/L0B/L0C/UB/...），`blockMmad` 拿到它才能在内部申请 L1/L0 buffer。

**② 把 GM 指针包装成 GlobalTensor**（[L114-L119](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L114-L119)）：

```cpp
AscendC::GlobalTensor<ElementA> gmA;
gmA.SetGlobalBuffer((__gm__ ElementA*)params.ptrA);
// gmB、gmC 同理
```

`GlobalTensor` 是 AscendC 对 GM 的封装，`gmA[offset]` 返回一个从 `offset` 开始的子视图——后面就用它来定位「本块对应的 A 子矩阵」。

**③ SPMD 步长循环 + 偏移计算 + 调用 blockMmad**（[L121-L138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121-L138)）：

```cpp
for (uint32_t loopIdx = AscendC::GetBlockIdx(); loopIdx < coreLoops; loopIdx += AscendC::GetBlockNum()) {
    GemmCoord blockCoord        = matmulBlockScheduler.GetBlockCoord(loopIdx);
    GemmCoord actualBlockShape  = matmulBlockScheduler.GetActualBlockShape(blockCoord);

    MatrixCoord offsetA{blockCoord.m() * L1TileShape::M, blockCoord.k() * L1TileShape::K};
    MatrixCoord offsetB{blockCoord.k() * L1TileShape::K, blockCoord.n() * L1TileShape::N};
    MatrixCoord offsetC{blockCoord.m() * L1TileShape::M, blockCoord.n() * L1TileShape::N};
    int64_t gmOffsetA = params.layoutA.GetOffset(offsetA);
    int64_t gmOffsetB = params.layoutB.GetOffset(offsetB);
    int64_t gmOffsetC = params.layoutC.GetOffset(offsetC);

    blockMmad(
        gmA[gmOffsetA], params.layoutA, gmB[gmOffsetB], params.layoutB,
        gmC[gmOffsetC], params.layoutC, actualBlockShape);
}

AscendC::PipeBarrier<PIPE_ALL>();
```

两个细节值得注意：

- `blockCoord.k()` 在 `GemmIdentityBlockSwizzle` 中恒为 0（BasicMatmul 不切 K，K 维的累加在 Block 层 `blockMmad` 内部完成），所以 `offsetA/offsetB` 的 K 分量这里实际是 0——K 维偏移发生在 Block 主循环里，不在 Kernel 层。
- 末尾 `PipeBarrier<PIPE_ALL>()` 让核内全部 8 条流水（MTE1/MTE2/M/MTE3/V/...）归位，确保本核所有异步搬运与计算完成后，控制流才返回。

`operator()<AIV>` 是空体（[L143-L145](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L143-L145)）：基础矩阵乘没有需要 Vector 核做的后处理，所以 AIV 什么都不干。当你以后接上 `BlockEpilogue`（激活、量化反量化等），这里就会被填充。

#### 4.2.4 代码实践

**实践目标**：用一个具体数值，追出 SPMD 循环把哪些块分给了哪个核。

**操作步骤**（纸上推演型）：

设 `problemShape = (M=256, N=512, K=1024)`，`L1TileShape = (128, 256, 256)`，假设物理核数 `GetBlockNum() = 4`。

1. `tileMN = (128, 256)`，`loopsMN = ceilDiv((256,512),(128,256)) = (2, 2)`，`coreLoops = 2*2 = 4`。
2. 列表写出 `GetBlockIdx()` ∈ {0,1,2,3} 时，每个核认领的 `loopIdx`。

**预期结果**：

| 核编号 GetBlockIdx() | 认领的 loopIdx |
| --- | --- |
| 0 | 0（循环到此结束，因为 0+4=4 ≥ coreLoops） |
| 1 | 1 |
| 2 | 2 |
| 3 | 3 |

此时恰好「块数 = 核数」，每核一块。若块数大于核数（例如把 N 放大到 1024，则 `loopsMN=(2,4)`、`coreLoops=8`），核 0 会认领 `0,4`，核 1 认领 `1,5`……依此类推。

**需要观察的现象**：无论块数与核数谁多谁少，这套步长循环都能保证「每块恰好被处理一次」，且各核工作量最多相差一块——这正是负载均衡的基础。

> 待本地验证：上述「最多相差一块」的结论，可在能跑 NPU 或 `--simulator` 仿真的环境里，通过在循环内打日志打印 `(GetBlockIdx(), loopIdx)` 来确认各核分块情况。

#### 4.2.5 小练习与答案

**练习 1**：若 `coreLoops = 10`、`GetBlockNum() = 4`，写出核 0、核 1 各自处理的 `loopIdx` 序列。
**答案**：核 0 处理 `0, 4, 8`；核 1 处理 `1, 5, 9`；核 2 处理 `2, 6`；核 3 处理 `3, 7`。

**练习 2**：为什么循环步长取 `GetBlockNum()` 而不是 1？若取 1 会怎样？
**答案**：步长取核数才能让各核**错开**认领不同的块，实现并行。若步长为 1，则每个核都会从 `GetBlockIdx()` 开始遍历到 `coreLoops`，导致同一个块被多个核重复计算，既浪费又会写出错误结果。

**练习 3**：`operator()<AIC>` 与 `operator()<AIV>` 为什么是两个特化？本讲例子里 AIV 为什么是空的？
**答案**：一颗 AI Core 分 AIC（Cube）和 AIV（Vector）两个子核，分别擅长矩阵乘与向量运算，所以按核类型特化分发逻辑。本例的 `BasicMatmul` 没有任何后处理（`BlockEpilogue = void`），Vector 核无事可做，故 AIV 体为空。

---

### 4.3 BlockScheduler 接口：块坐标、遍历顺序与边界裁剪

#### 4.3.1 概念说明

SPMD 循环把「编号 loopIdx」交给每个核，但「编号 loopIdx 对应 C 矩阵的哪个块」是由 **`BlockScheduler`** 决定的。它其实还悄悄承担了两个重要职责，是本模块的重点：

1. **遍历顺序（Swizzle）**：决定按什么顺序遍历 C 的基本块网格。顺序不同，会影响 L1 缓存命中率与多核读取冲突。`GemmIdentityBlockSwizzle` 用 `SwizzleOffset / SwizzleDirection` 两个模板参数刻画一种「之字形（Z 字）」遍历。
2. **边界裁剪（ActualBlockShape）**：当 M、N 不是 `L1TileShape` 的整数倍时，最后一行/最后一列的块会「缺角」。`GetActualBlockShape` 负责把这种缺角块的真实尺寸算出来，传给 `blockMmad`，避免越界读写。

`BasicMatmul` 默认用的是 `GemmIdentityBlockSwizzle<>`（即 `SwizzleOffset=1, SwizzleDirection=0`），定义在 [include/catlass/gemm/block/block_swizzle.hpp:24-127](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L24-L127)。

#### 4.3.2 核心流程

调度器在构造时就把「问题尺寸」和「tile 尺寸」换算成「块网格尺寸」`loopsMN`，之后三个接口各自负责一件事：

```
构造：  loopsMN = CeilDiv( (M,N), tileMN )          // M/N 方向各有多少块

GetCoreLoops()        → loopsMN.row * loopsMN.column           // 块总数，即 SPMD 循环上界
GetBlockCoord(taskIdx)→ (mIdx, nIdx, 0)                         // 第 taskIdx 块在网格中的坐标
GetActualBlockShape(c)→ (mActual, nActual, K)                   // 触边块的真实尺寸（裁剪）
```

以默认 `SwizzleOffset=1` 为例，遍历顺序退化为最朴素的行主序：`mIdx = taskIdx / loopsMN.column`，`nIdx = taskIdx % loopsMN.column`（推导见下文）。`SwizzleOffset>1` 时会按「每 `SwizzleOffset` 行组成一个 tileBlock、奇数 tileBlock 反向」的方式做 Z 字遍历，以提升局部性。

#### 4.3.3 源码精读

**① 构造与 loopsMN**（[block_swizzle.hpp:38-43](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L38-L43)）：

```cpp
GemmIdentityBlockSwizzle(GemmCoord const& problemShape_, MatrixCoord const& tileMN_)
    : problemShape(problemShape_), tileMN(tileMN_)
{
    loopsMN = CeilDiv(MatrixCoord(problemShape.GetCoordMN()), tileMN);
}
```

`CeilDiv` 对 `(M,N)` 与 `tileMN` 做逐维向上取整。例如 `(256,512)` 与 `(128,256)` 得到 `(2,2)`。

**② GetCoreLoops**（[L67-L71](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L67-L71)）：返回块总数 `loopsMN.row * loopsMN.column`，这正是 `operator()<AIC>` 里 SPMD 循环的上界 `coreLoops`。

**③ GetBlockCoord**（[L79-L114](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L79-L114)）：把一维 `taskIdx` 映射成二维 `(mIdx, nIdx)`。当 `SwizzleDirection==0`（Zn 方向）时：

```cpp
uint32_t innerIdx = taskIdx % GetCoreLoops();
uint32_t tileBlockLoop = CeilDiv(loopsMN.row(), SwizzleOffset);
uint32_t tileBlockIdx  = innerIdx / (SwizzleOffset * loopsMN.column());
uint32_t inTileBlockIdx= innerIdx % (SwizzleOffset * loopsMN.column());
// ...（奇偶 tileBlock 反向，得到 Z 字遍历）
return GemmCoord{mIdx, nIdx, 0};
```

代入默认 `SwizzleOffset=1`：`tileBlockIdx = innerIdx / loopsMN.column()`、`inTileBlockIdx = innerIdx % loopsMN.column()`，于是 `mIdx = tileBlockIdx`、`nIdx = inTileBlockIdx`——也就是行主序遍历。返回的 K 分量恒为 0，呼应 4.2 节「Kernel 层不切 K」。

> 说明：`SwizzleOffset>1` 的 Z 字效果属于 U4「Swizzle 策略」讲义的范畴，本讲只需理解「它决定遍历顺序，且默认值等价于行主序」即可。

**④ GetActualBlockShape**（[L116-L126](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L116-L126)）：处理「触边裁剪」：

```cpp
GemmCoord GetActualBlockShape(GemmCoord blockCoord)
{
    uint32_t mActual = (blockCoord.m() == (loopsMN.row() - 1)) ?
                        (problemShape.m() - blockCoord.m() * tileMN.row()) : tileMN.row();
    uint32_t nActual = (blockCoord.n() == (loopsMN.column() - 1)) ?
                        (problemShape.n() - blockCoord.n() * tileMN.column()) : tileMN.column();
    uint32_t kActual = problemShape.k();
    return GemmCoord{mActual, nActual, kActual};
}
```

逻辑很直白：只有「最后一行/最后一列」的块才可能缺角，此时真实尺寸 = `问题尺寸 - 前面整块占据的部分`；其余块都是完整 tile。K 维则恒为完整 `problemShape.k()`（因为 Kernel 层不切 K）。

回到 `operator()<AIC>`，这三个接口串起来的一段就是 [basic_matmul.hpp:121-138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121-L138)。一个完整的工作样例（接 4.2.4 的数值，`problemShape=(256,512,1024)`，`L1TileShape=(128,256,256)`）：

- 某核拿到 `loopIdx=3` → `GetBlockCoord(3)` = `(mIdx=1, nIdx=1, 0)`
- `offsetA = (1*128, 0) = (128, 0)`；`offsetC = (1*128, 1*256) = (128, 256)`
- 若 A 是 RowMajor `(M,K)=(256,1024)`，`stride=(1024,1)`，则 `gmOffsetA = 128*1024 + 0 = 131072`
- 若 C 是 RowMajor `(M,N)=(256,512)`，`stride=(512,1)`，则 `gmOffsetC = 128*512 + 256 = 65792`
- `GetActualBlockShape((1,1,0))`：因为 `(256,512)` 是 `(128,256)` 的整数倍，无缺角，返回 `(128, 256, 1024)`
- 于是调用 `blockMmad(gmA[131072], layoutA, ..., gmC[65792], layoutC, (128,256,1024))`

整条「块坐标 → 逻辑偏移 → GM 字节偏移 → blockMmad」的链路就此闭合。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：在 `operator()<AIC>` 中完整追踪一个基本块从 `GetBlockCoord` 到 `blockMmad` 调用的全路径。

**操作步骤**（源码阅读 + 纸上追踪型）：

1. 打开 [basic_matmul.hpp 的 operator()\<AIC\>](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L104-L141)。
2. 设 `problemShape=(M=300, N=400, K=512)`，`L1TileShape=(128,256,256)`（注意 M、N 都**不是** tile 的整数倍，制造缺角）。
3. 计算：`loopsMN`、`coreLoops`、并对 `loopIdx=2` 求出 `blockCoord`、`actualBlockShape`、`offsetC`、`gmOffsetC`（假设 C 为 RowMajor）。

**预期结果**（逐步）：

- `loopsMN = ceilDiv((300,400),(128,256)) = (3, 2)`（300/128 向上取整 = 3，400/256 向上取整 = 2）。
- `coreLoops = 3 * 2 = 6`。
- 默认行主序遍历：`loopIdx=2 → mIdx = 2/2 = 1`，`nIdx = 2%2 = 0`，即 `blockCoord = (1, 0, 0)`。
- `offsetC = (1*128, 0*256) = (128, 0)`；C 为 RowMajor `(300,400)`、`stride=(400,1)`，故 `gmOffsetC = 128*400 + 0 = 51200`。
- `actualBlockShape`：`blockCoord.m()==1` 不是最后一行（最后一行 mIdx=2），所以 `mActual=128`；`blockCoord.n()==0` 不是最后一列（最后一列 nIdx=1），所以 `nActual=256`；`kActual=512`。结果 `(128, 256, 512)`，完整块。
- 作为对照，`loopIdx=5 → mIdx=2, nIdx=1`，这是**最右下角**的缺角块：`mActual = 300 - 2*128 = 44`，`nActual = 400 - 1*256 = 144`，`actualBlockShape = (44, 144, 512)`——`blockMmad` 就靠这个裁剪后的尺寸避免越界。

**需要观察的现象**：

- 完整块（非触边）的 `actualBlockShape` 恰为 `(L1TileShape::M, L1TileShape::N, K)`。
- 触边块的 `actualBlockShape` 在 M 或 N 维上小于 tile 尺寸，差值正是「问题尺寸对 tile 取模的余数」。
- 这解释了为什么 `blockMmad` 的最后一个参数是 `actualBlockShape` 而不是固定的 `L1TileShape`：它必须按真实尺寸搬运和计算。

> 待本地验证：可在仿真或 NPU 环境下，把 `(M,N,K)` 设成上述 `(300,400,512)` 跑 `00_basic_matmul`，确认仍输出 `Compare success.`——这验证了触边裁剪逻辑的正确性。

#### 4.3.5 小练习与答案

**练习 1**：`GetCoreLoops()` 返回的是「块数」还是「元素数」？它和 `GetBlockNum()`（核数）是什么关系？
**答案**：返回的是「基本块个数」=`loopsMN.row * loopsMN.column`。它是 SPMD 循环的上界；当块数 > 核数时一个核处理多块，当块数 < 核数时部分核空闲。

**练习 2**：`problemShape=(300,400,512)`、`L1TileShape=(128,256,256)` 时，最右下角块的 `actualBlockShape` 是多少？为什么需要它？
**答案**：`(44, 144, 512)`。因为 300 不是 128 的整数倍、400 不是 256 的整数倍，最右下角块是缺角的，真实大小只有 44×144；若仍按完整 tile (128,256) 处理会越界读写 GM，所以必须用裁剪后的尺寸。

**练习 3**：默认 `GemmIdentityBlockSwizzle<>`（`SwizzleOffset=1`）的遍历顺序是什么？把 `loopIdx=0..5` 映射到 `(mIdx,nIdx)`（`loopsMN=(3,2)`）。
**答案**：行主序——mIdx 为外层、nIdx 为内层。`0→(0,0), 1→(0,1), 2→(1,0), 3→(1,1), 4→(2,0), 5→(2,1)`。

## 5. 综合实践

把三个模块串起来，完成一次「纸面端到端推演」。

**任务**：给定 `problemShape = (M=300, N=400, K=512)`、`L1TileShape = GemmShape<128,256,256>`、A/B/C 均为 RowMajor、物理核数 `GetBlockNum() = 4`，请完成下表，并回答两个问题。

1. 计算 `loopsMN` 与 `coreLoops`。
2. 对核 0（`GetBlockIdx()=0`）列出它认领的所有 `loopIdx`，并逐一给出 `blockCoord`、`actualBlockShape`、`gmOffsetC`（C 为 RowMajor `(300,400)`）。

**参考解答**：

1. `loopsMN = ceilDiv((300,400),(128,256)) = (3,2)`，`coreLoops = 6`。
2. 核 0 步长为 4，认领 `loopIdx = 0, 4`：

| loopIdx | blockCoord (m,n,k) | actualBlockShape | gmOffsetC = mIdx*128*400 + nIdx*256 |
| --- | --- | --- | --- |
| 0 | (0,0,0) | (128, 256, 512) | 0*400 + 0 = 0 |
| 4 | (2,0,0) | (44, 256, 512) | 2*128*400 + 0 = 102400 |

（`loopIdx=4` 的 mIdx=4/2=2 是最后一行，故 mActual=300-2*128=44；nIdx=0 非末列，nActual=256。）

**思考题**（开放）：若把 `GetBlockNum()` 翻倍到 8，而 `coreLoops` 仍为 6，会发生什么？——核 0..5 各处理一块、核 6、7 空闲（循环条件 `loopIdx < 6` 一开始就不满足）。这正是「块数少于核数时部分核空闲」的现象，也是后续 SplitK（u8-l2）想通过切 K 来制造更多块、提升利用率的动机。

## 6. 本讲小结

- `BasicMatmul` 用「`Arguments`（用户视图）+ `Params`（内核视图）」两层结构隔离用户接口与实现细节；`ToUnderlyingArguments` 用 `MakeLayout` 把尺寸补全成 Layout，是所有 GM 偏移计算的地基。
- 设备侧 `operator()<AIC>` 用**步长循环** `for(loopIdx=GetBlockIdx(); ...; loopIdx+=GetBlockNum())` 实现多核分块——起点是核号、步长是核数，天然保证每块恰好被一个核处理。
- `BlockScheduler`（默认 `GemmIdentityBlockSwizzle`）提供三件套：`GetCoreLoops` 给循环上界、`GetBlockCoord` 把一维块号映射成二维网格坐标（含 Swizzle 遍历顺序）、`GetActualBlockShape` 做触边裁剪。
- 每个块的执行链路是：`GetBlockCoord → blockCoord*L1TileShape 得 offsetX → layout.GetOffset 得 gmOffset → gmX[gmOffset] 传入 blockMmad`，K 维偏移发生在 Block 层而非 Kernel 层。
- AIC 负责矩阵乘、AIV 在本例为空（无后处理）；末尾 `PipeBarrier<PIPE_ALL>()` 保证核内所有流水归位。
- 本讲止于「把单个 C 块交给 `blockMmad`」；`blockMmad` 内部的 K 维主循环、GM→L1→L0 搬运与 `Mmad` 指令，留给 U4「Block 层与主循环」。

## 7. 下一步学习建议

- **进入 Block 层**：阅读 [include/catlass/gemm/block/block_mmad.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp) 与 [block_mmad_pingpong.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp)，看 K 维主循环如何用多缓冲隐藏搬运延迟——对应大纲 u4-l1。
- **深入 Swizzle**：想理解 `SwizzleOffset>1` 的 Z 字遍历为何能提升缓存命中，读 [block_swizzle.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp) 中 `SwizzleDirection` 分支与文档 `docs/zh/2_Design/01_kernel_design/02_swizzle.md`——对应 u4-l4。
- **理解切 K**：当「块数 < 核数」造成核空闲时，SplitK/StreamK 通过在 K 维再切块来制造更多并行度，阅读 [splitk_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/splitk_matmul.hpp) 与对应的 `SplitkGemmIdentityBlockSwizzle`——对应 u8-l2。
