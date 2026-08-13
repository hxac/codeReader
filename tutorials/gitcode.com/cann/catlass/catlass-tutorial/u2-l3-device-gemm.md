# Device 层适配器 DeviceGemm

## 1. 本讲目标

上一讲（u2-l2）我们把 `00_basic_matmul` 那串 `using` 别名串成了「五步组装」，并停在最后一步 `DeviceGemm`——知道它把 Kernel 包装成 Host 可调用的对象。本讲就专门拆开这个包装盒。

学完本讲你应该能够：

- 说出 `DeviceGemm` 的 `CanImplement` / `GetWorkspaceSize` / `Initialize` / `operator()` 四个接口各自做什么，以及它们如何把工作**转发**给 Kernel。
- 解释为什么要有 `Arguments`（用户 API）和 `Params`（Kernel API）两套结构，以及 `ToUnderlyingArguments` 在两者之间扮演什么角色。
- 说出 workspace 的「计算大小 → 申请显存 → 传入 Kernel → 用完释放」完整生命周期，并理解 `GetWorkspaceSize` 返回非 0 时 Host 该怎么处理。

核心一句话：`DeviceGemm` 是一个**很薄的适配器**，它本身几乎没有逻辑，真正的活儿全在 Kernel 里；它的价值是把 Host 调用约定固定下来，屏蔽不同 Kernel、不同硬件之间的设备侧差异。

## 2. 前置知识

- **五层抽象与五步组装**（u1-l1、u2-l2）：Device→Kernel→Block→Tile→Basic，以及 `BlockMmad → BlockEpilogue/BlockScheduler → BasicMatmul → DeviceGemm` 的组装链。
- **ACL 运行时三件套**（u2-l1）：`aclInit` / `aclrtSetDevice` / `aclrtCreateStream` 建立运行时，`aclrtMalloc` 申请 GM 显存，`aclrtMemcpy` 在 Host 与 Device 间搬数据。本讲出现的 `aclrtStream` 就是其中创建的流。
- **Layout 与 problemShape**（u3-l1 会详细讲，这里只需知道）：布局对象（`LayoutA/LayoutB/LayoutC`）记录矩阵的排布与步长，可由 `problemShape`（M/N/K）+ 元素类型构造出来。
- **SPMD 多核模型**（u1-l2）：所有核跑同一份 kernel，靠 `GetBlockIdx()/GetBlockNum()` 认领不同任务块；Host 派发时要告诉运行时启动多少个核（`blockDim`）。

> 一个术语提醒：本讲里反复出现的 `workspace`，指算子在 GM 上额外需要的一块临时显存（比如 SplitK 的中间累加结果）。它和「矩阵 A/B/C 的显存」是两回事。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/catlass/gemm/device/device_gemm.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp) | 本讲主角。`DeviceGemm` 模板类的全部定义，只有约 100 行，是一个薄适配器。 |
| [include/catlass/gemm/kernel/basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp) | `BasicMatmul` Kernel。定义了 `Arguments`/`Params`、`CanImplement`/`GetWorkspaceSize`/`ToUnderlyingArguments`，以及设备侧 SPMD 主循环。本讲用来对照「转发目标」。 |
| [include/catlass/gemm/kernel/splitk_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/splitk_matmul.hpp) | `SplitkMatmul` Kernel。是 `GetWorkspaceSize` 返回非 0 的典型例子，本讲用来讲 workspace 管理。 |
| [include/catlass/detail/kernel_adapter.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/detail/kernel_adapter.hpp) | `KernelAdapter` 模板：把任意 Kernel 的 `Params` 包装成可派发的全局入口。 |
| [include/catlass/status.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/status.hpp) | `Status` 枚举（`kSuccess` / `kInvalid`），接口返回值类型。 |
| [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp) | Host 侧调用方。展示 `DeviceGemm` 四接口的标准调用顺序。 |

## 4. 核心概念与源码讲解

### 4.1 DeviceGemm 接口：一个会转发的薄适配器

#### 4.1.1 概念说明

`DeviceGemm` 是 Device 层（五层抽象的最上层）的唯一对外类。它的职责不是「自己计算」，而是**把 Host 的调用约定固定下来，再把每一步具体工作转交给 Kernel 去做**。

为什么需要这样一个中间层？

- **Host 调用约定要稳定**：无论底层用的是 `BasicMatmul` 还是 `SplitkMatmul`、跑在 AtlasA2 还是 Ascend950，Host 侧应该用同一套 `CanImplement → GetWorkspaceSize → Initialize → operator()` 调用流程。这样换 Kernel 时 Host 代码几乎不用动。
- **设备侧差异要屏蔽**：不同架构的 kernel 派发方式（核数、同步原语）不同，这些差异封装在 `Run()` 内部的 `#if CATLASS_ARCH` 分支里，对 Host 透明。
- **Kernel 专注算子逻辑**：Kernel 只关心「拿到 Params 怎么算」，不必操心显存申请、流调度等运行时杂事。

这正是「适配器（Adapter）」模式的典型用法：`DeviceGemm` 适配「Host 调用者」与「Kernel 被调者」两个接口。

#### 4.1.2 核心流程

`DeviceGemm` 的四个接口构成一条标准的 Host 调用流水线：

```text
┌─────────────────────────────────────────────────────────────┐
│  Host 调用方（basic_matmul.cpp）                            │
└─────────────────────────────────────────────────────────────┘
   │
   │  1. 构造 Arguments（problemShape + 3 个 GM 指针）
   ▼
┌─────────────────────────┐        转发
│ DeviceGemm.CanImplement │ ─────────────────────►  Kernel::CanImplement
└─────────────────────────┘
   │
   ▼
┌──────────────────────────┐       转发
│ DeviceGemm.GetWorkspace  │ ─────────────────────►  Kernel::GetWorkspaceSize
│       Size               │        返回字节数
└──────────────────────────┘
   │
   │  Host 据此申请 GM 显存（仅当 > 0）
   ▼
┌──────────────────────────┐       转发
│ DeviceGemm.Initialize    │ ─────────────────────►  Kernel::ToUnderlyingArguments
│                          │        Arguments → Params，结果存入 params_
└──────────────────────────┘
   │
   ▼
┌──────────────────────────┐       派发
│ DeviceGemm.operator()    │ ─────────────────────►  KernelAdapter<<<blockDim>>>(params_)
│                          │        启动 blockDim 个核执行 Kernel
└──────────────────────────┘
```

关键点：前三个接口都在**转发**，只有 `operator()` 负责把构造好的 `params_` **派发**到 NPU 上执行。

#### 4.1.3 源码精读

整个类非常短，先看它的骨架和类型成员：

[include/catlass/gemm/device/device_gemm.hpp:21-44](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L21-L44) —— `DeviceGemm` 的模板参数只有一个 `GemmKernel`；它从 Kernel 里「借」出两个类型 `Arguments` 和 `Params`，并持有一个 `Params params_` 成员作为内部状态。

```cpp
template <class GemmKernel>
class DeviceGemm {
public:
    using Kernel = GemmKernel;
    using Arguments = typename GemmKernel::Arguments;  // 用户 API
    using Params    = typename GemmKernel::Params;     // Kernel API
private:
    Params params_;   // 唯一的运行时状态
    ...
};
```

四个接口的转发实现，逻辑都极简：

[include/catlass/gemm/device/device_gemm.hpp:47-70](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L47-L70) —— `CanImplement` 把 Kernel 的布尔返回值映射成 `Status`；`GetWorkspaceSize` 直接返回 Kernel 算出的字节数；`Initialize` 调用 `ToUnderlyingArguments` 完成转换并把结果存进 `params_`。

```cpp
static Status CanImplement(Arguments const& args) {
    return GemmKernel::CanImplement(args) ? Status::kSuccess : Status::kInvalid;
}
static size_t GetWorkspaceSize(Arguments const& args) {
    return GemmKernel::GetWorkspaceSize(args);
}
Status Initialize(Arguments const& args, uint8_t* workspace = nullptr,
                  aclrtStream stream = nullptr) {
    params_ = GemmKernel::ToUnderlyingArguments(args, workspace);
    return Status::kSuccess;
}
```

真正「干活」的是 `operator()` → `Run()`，它负责把 `params_` 派发到硬件：

[include/catlass/gemm/device/device_gemm.hpp:74-97](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L74-L97) —— `Run()` 用 `<<<blockDim, nullptr, stream>>>` 这一 AscendC 派发语法启动 kernel，并按 `CATLASS_ARCH`（2201/3510）区分是否传入跨核同步地址。

```cpp
inline Status Run(aclrtStream stream, uint32_t blockDim, uint64_t hardwareSyncAddr) {
#if (CATLASS_ARCH == 2201)
    if (hardwareSyncAddr == 0) {
        Catlass::KernelAdapter<GemmKernel><<<blockDim, nullptr, stream>>>(params_);
    } else {
        Catlass::KernelAdapter<GemmKernel><<<blockDim, nullptr, stream>>>(params_, hardwareSyncAddr);
    }
#elif (CATLASS_ARCH == 3510)
    Catlass::KernelAdapter<GemmKernel><<<blockDim, nullptr, stream>>>(params_);
#endif
    return Status::kSuccess;
}
inline Status operator()(aclrtStream stream, uint32_t blockDim) {
    return Run(stream, blockDim, 0);   // 默认 hardwareSyncAddr=0
}
```

几点说明：

- `blockDim` 就是 Host 传进来的 AIC 核数（见 4.1.4 的样例代码），决定启动多少个核跑 SPMD 循环。
- `KernelAdapter` 是个全局模板函数（见 4.1.4），它接收 `params_` 后构造 Kernel 对象并调用其 `operator()`。
- `hardwareSyncAddr` 是 AtlasA2 上的跨核同步基地址，普通样例传 0；只有需要 AIC/AIV 跨核握手（如 SplitK）时才传非 0 值。这部分设备侧差异被封装在此，Host 调用方无感。

#### 4.1.4 代码实践

**实践目标**：把 `basic_matmul.cpp` 中调用 `DeviceGemm` 四接口的 6 行代码，与上面的转发流水线一一对应，确认「Host 只调接口、不碰内部」。

**操作步骤**：

1. 打开 [examples/00_basic_matmul/basic_matmul.cpp:105-119](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L105-L119)。
2. 逐行标注它调用了 `DeviceGemm` 的哪个接口：

```cpp
using MatmulAdapter = Gemm::Device::DeviceGemm<MatmulKernel>;
MatmulKernel::Arguments arguments{options.problemShape, deviceA, deviceB, deviceC}; // 构造 Arguments
MatmulAdapter matmulOp;
matmulOp.CanImplement(arguments);          // ① 能力检查
size_t sizeWorkspace = matmulOp.GetWorkspaceSize(arguments);  // ② 算 workspace 大小
uint8_t* deviceWorkspace = nullptr;
if (sizeWorkspace > 0) { ... aclrtMalloc ... }   // ③ 按需申请
matmulOp.Initialize(arguments, deviceWorkspace);  // ④ 转换并存 params_
matmulOp(stream, aicCoreNum);                     // ⑤ 派发执行
ACL_CHECK(aclrtSynchronizeStream(stream));        // ⑥ Host 等待完成
```

**需要观察的现象**：注意 ⑤ `matmulOp(stream, aicCoreNum)` 是异步的——派发后立即返回，必须靠 ⑥ `aclrtSynchronizeStream` 才能保证算完。这和 u2-l1 讲的「ACL 流是异步的」一致。

**预期结果**：你能清楚指出每一行对应流水线里的哪一步，并理解 Host 全程没出现 `params_`、`KernelAdapter`、`<<<>>>` 这些内部细节——它们都被 `DeviceGemm` 封装掉了。

#### 4.1.5 小练习与答案

**练习 1**：`DeviceGemm` 类里唯一的非静态数据成员是什么？为什么只需要它？

> **答案**：是 `Params params_`（[device_gemm.hpp:32](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L32)）。因为 `DeviceGemm` 是无状态适配器，唯一需要在接口之间「携带」的信息就是转换好的 `Params`：`Initialize` 写入它，`operator()` 读出它派发。

**练习 2**：`CanImplement` 和 `GetWorkspaceSize` 为什么是 `static`，而 `Initialize` 和 `operator()` 不是？

> **答案**：前两者只依赖入参 `Arguments`，不读写对象状态，故可 `static`，能脱离实例直接调用；后两者要读写成员 `params_`，必须绑定实例。

---

### 4.2 Arguments 与 Params：用户视图与内核视图的分离

#### 4.2.1 概念说明

`DeviceGemm` 转发时反复出现的两个结构 `Arguments` 和 `Params`，是 CATLASS 设计中的一对关键概念：

- **`Arguments`（用户 API）**：Host 调用方需要提供的东西，尽量精简——通常只有 `problemShape`（M/N/K）和几个 GM 指针。它的设计目标是「让用户少操心」。
- **`Params`（Kernel API）**：设备侧 kernel 真正需要的东西，内容更丰富——除了指针和 problemShape，还包含预先构造好的 `LayoutA/LayoutB/LayoutC`，必要时还包含 `ptrWorkspace`、`splitkFactor` 等。

为什么要分两层？因为**用户知道的和 kernel 需要的不一样**。用户只关心「我要算多大的矩阵、输入输出在哪」；而 kernel 在设备侧要频繁用布局的步长信息去算偏移（如 `params.layoutA.GetOffset(offsetA)`）。把「由 problemShape 构造 layout」这件事交给 `ToUnderlyingArguments` 集中完成，既让用户接口干净，又避免设备侧重复构造。

#### 4.2.2 核心流程

```text
  Arguments（Host 构造）                  Params（设备侧使用）
  ┌───────────────────┐                  ┌──────────────────────────┐
  │ problemShape      │   ToUnderlying    │ problemShape             │
  │ ptrA, ptrB, ptrC  │ ──Arguments()──► │ ptrA, layoutA            │
  │ (+ splitk 的核数等)│   构造 3 个 Layout│ ptrB, layoutB            │
  └───────────────────┘                  │ ptrC, layoutC            │
                                          │ (+ ptrWorkspace 等)      │
                                          └──────────────────────────┘
            ▲                                       │
            │                                       │
   Host 提供（少）                          存入 params_，随派发进 NPU
```

桥接动作只发生一次，在 `Initialize` 里：`params_ = GemmKernel::ToUnderlyingArguments(args, workspace)`。

#### 4.2.3 源码精读

先看 `BasicMatmul` 的两层结构对比。`Arguments` 极简，只有 4 个字段：

[include/catlass/gemm/kernel/basic_matmul.hpp:69-74](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L69-L74)

```cpp
struct Arguments {
    GemmCoord problemShape;
    GM_ADDR ptrA;
    GM_ADDR ptrB;
    GM_ADDR ptrC;
};
```

而 `Params` 多出 3 个 Layout 字段（其余字段一一对应）：

[include/catlass/gemm/kernel/basic_matmul.hpp:40-67](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L40-L67) —— `Params` 把 `layoutA/layoutB/layoutC` 与各自的指针成对存放，供设备侧循环里算偏移用。

桥接函数 `ToUnderlyingArguments` 是「由 problemShape 生成 Layout」的唯一发生地：

[include/catlass/gemm/kernel/basic_matmul.hpp:86-93](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L86-L93)

```cpp
static Params ToUnderlyingArguments(const Arguments& args, uint8_t* workspace) {
    LayoutA layoutA = LayoutA::template MakeLayout<ElementA>(args.problemShape.m(), args.problemShape.k());
    LayoutB layoutB = LayoutB::template MakeLayout<ElementB>(args.problemShape.k(), args.problemShape.n());
    LayoutC layoutC = LayoutC::template MakeLayout<ElementC>(args.problemShape.m(), args.problemShape.n());
    Params params{args.problemShape, args.ptrA, layoutA, args.ptrB, layoutB, args.ptrC, layoutC};
    return params;
}
```

注意第二个参数 `workspace`：本例（`BasicMatmul`）完全没用它，但签名保留，是为了让 `DeviceGemm::Initialize` 能用统一接口调用所有 Kernel。对于需要 workspace 的 Kernel（如 SplitK），这个指针会被写进 `Params.ptrWorkspace`（见 4.3）。

转换好的 `Params` 在设备侧主循环里被直接消费，例如这一行用 `params.layoutA` 算 GM 偏移：

[include/catlass/gemm/kernel/basic_matmul.hpp:130-137](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L130-L137)

```cpp
int64_t gmOffsetA = params.layoutA.GetOffset(offsetA);
...
blockMmad(gmA[gmOffsetA], params.layoutA, gmB[gmOffsetB], params.layoutB,
          gmC[gmOffsetC], params.layoutC, actualBlockShape);
```

这正说明为什么 `Params` 要带 Layout：设备侧每算一个块都要 `GetOffset`，布局必须现成可用。

#### 4.2.4 代码实践

**实践目标**：理解「用户只填 Arguments、Kernel 自己补 Layout」的分工，亲手追一次数据流向。

**操作步骤**：

1. 在 [basic_matmul.cpp:106](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L106) 确认 `arguments` 只给了 `{problemShape, deviceA, deviceB, deviceC}` 四项，没有任何 Layout。
2. 在 [basic_matmul.hpp:86-93](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L86-L93) 确认三个 Layout 是在 `ToUnderlyingArguments` 里现造的。
3. 思考：如果让你在 Host（`basic_matmul.cpp`）里就构造好 Layout 再塞进 `Arguments`，会有什么坏处？

**预期结果**：你会得出「集中构造更省心、避免用户写错布局」的结论；同时注意到 `ToUnderlyingArguments` 是 Host 函数（不带 `CATLASS_DEVICE`），它在 `Initialize` 阶段于 Host 侧执行，构造结果随 `params_` 一起被拷贝进设备。

> 待本地验证：若想确认 `ToUnderlyingArguments` 确实在 Host 侧执行，可在该函数内加一行 `std::cout` 打印 problemShape（示例代码，仅供调试，勿提交），编译运行后应看到它在 `Initialize` 调用时、kernel 派发之前打印。

#### 4.2.5 小练习与答案

**练习 1**：`BasicMatmul::Arguments` 和 `Params` 的字段差异，正好体现了「用户视图 vs 内核视图」。请列出 `Params` 比 `Arguments` 多出的字段。

> **答案**：多出 `layoutA`、`layoutB`、`layoutC` 三个布局对象。`SplitkMatmul` 的 `Params` 还会多出 `ptrWorkspace` 和 `splitkFactor`（见 4.3）。

**练习 2**：为什么 `ToUnderlyingArguments` 要接收一个 `workspace` 参数，即使 `BasicMatmul` 用不到它？

> **答案**：为了让 `DeviceGemm::Initialize` 能用同一句 `params_ = Kernel::ToUnderlyingArguments(args, workspace)` 调用所有 Kernel（[device_gemm.hpp:68](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L68)）。统一签名是适配器屏蔽 Kernel 差异的前提；不需要 workspace 的 Kernel 忽略它即可。

---

### 4.3 Workspace 管理：从计算大小到传递释放

#### 4.3.1 概念说明

很多算子除了输入 A/B 和输出 C，还需要一块**额外的 GM 临时显存**来完成计算，这块显存就叫 **workspace**。典型场景是 SplitK：把 K 维切成多份分给多个核并行算，每个核只得到部分和，必须先写到一个临时区域，最后再归约（ReduceAdd）成最终 C。这个临时区域就是 workspace。

CATLASS 把 workspace 的管理完全交给 Host 与 `DeviceGemm` 协作完成，分四步：

1. **算大小**：`GetWorkspaceSize(Arguments)` 返回需要的字节数。
2. **申请**：Host 用 `aclrtMalloc` 申请这块 GM（**仅当大小 > 0**）。
3. **传入**：把指针作为 `Initialize` 的第二个参数，经 `ToUnderlyingArguments` 写进 `Params.ptrWorkspace`，随派发进入 kernel。
4. **释放**：kernel 执行完（`aclrtSynchronizeStream` 之后），Host 用 `aclrtFree` 释放。

为什么用「按需申请」而不是固定分配？因为不同算子、不同 problemShape 需要的 workspace 差异巨大（基础 GEMM 是 0，大 K 的 SplitK 可能是几百 MB），按需申请避免浪费显存。

#### 4.3.2 核心流程

```text
GetWorkspaceSize(args) ──► sizeWorkspace（字节数）
        │
        ├─ sizeWorkspace == 0 ─► deviceWorkspace 保持 nullptr，跳过申请
        │
        └─ sizeWorkspace  > 0  ─► aclrtMalloc 申请 GM
                                    │
                Initialize(args, deviceWorkspace)
                                    │  ToUnderlyingArguments 把 deviceWorkspace 写进 Params.ptrWorkspace
                                    ▼
                operator()(stream, blockDim)  ── 派发 ──► kernel 用 ptrWorkspace 读/写中间结果
                                    │
                aclrtSynchronizeStream(stream)  ── 等算完
                                    │
                if (sizeWorkspace > 0) aclrtFree(deviceWorkspace)
```

对于 SplitK，workspace 大小可由公式估算（`workspaceElementSize` 通常为累加类型 `ElementAccumulator` 的字节数，如 fp32 = 4）：

\[ \text{workspaceBytes} = \text{workspaceElementSize} \times M \times N \times \text{splitkFactor} \]

其中 `splitkFactor` 是 K 维切分数，由 `GetSplitkFactor` 根据 M/N/K 与核数动态决定。

#### 4.3.3 源码精读

先看 `BasicMatmul` 的「零 workspace」情形，作为对照基准：

[include/catlass/gemm/kernel/basic_matmul.hpp:81-84](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L81-L84) —— 直接返回 0，因为每个 C 块由单个 AIC 一次算完，无需中间存储。

```cpp
static size_t GetWorkspaceSize(const Arguments& args) { return 0; }
```

再看 `SplitkMatmul` 的「非零 workspace」情形。它的 `Arguments` 多了 `aicCoreNum` 和 `workspaceElementSize` 两个字段（这是「用户视图」随算子变复杂的体现）：

[include/catlass/gemm/kernel/splitk_matmul.hpp:193-200](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/splitk_matmul.hpp#L193-L200)

```cpp
struct Arguments {
    GemmCoord problemShape;
    uint32_t aicCoreNum;          // 核数，用于算 splitkFactor
    size_t workspaceElementSize;  // 累加元素字节数
    GM_ADDR ptrA, ptrB, ptrC;
};
```

`GetWorkspaceSize` 按上面公式计算：

[include/catlass/gemm/kernel/splitk_matmul.hpp:255-259](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/splitk_matmul.hpp#L255-L259)

```cpp
static size_t GetWorkspaceSize(const Arguments& args) {
    return args.workspaceElementSize * args.problemShape.m() * args.problemShape.n() *
           GetSplitkFactor(args.problemShape.m(), args.problemShape.n(), args.problemShape.k(), args.aicCoreNum);
}
```

它的 `Params` 相应多出 `ptrWorkspace` 与 `splitkFactor`：

[include/catlass/gemm/kernel/splitk_matmul.hpp:169-170](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/splitk_matmul.hpp#L169-L170)

```cpp
GM_ADDR ptrWorkspace;
uint32_t splitkFactor = 1;
```

`ToUnderlyingArguments` 把 Host 申请好的 workspace 指针写入 `Params`——这正是 workspace 从 Host 流入 kernel 的关键一步：

[include/catlass/gemm/kernel/splitk_matmul.hpp:261-277](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/splitk_matmul.hpp#L261-L277)

```cpp
static Params ToUnderlyingArguments(const Arguments& args, uint8_t* workspace) {
    ...
    Params params{..., workspace,
                  GetSplitkFactor(args.problemShape.m(),..., args.aicCoreNum)};
    return params;
}
```

回到 Host 侧，`basic_matmul.cpp` 用 `if (sizeWorkspace > 0)` 守卫来「按需申请/释放」，这是处理非零 workspace 的标准写法：

[examples/00_basic_matmul/basic_matmul.cpp:109-119](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L109-L119)

```cpp
size_t sizeWorkspace = matmulOp.GetWorkspaceSize(arguments);
uint8_t* deviceWorkspace = nullptr;
if (sizeWorkspace > 0) {                                  // 仅当需要时申请
    ACL_CHECK(aclrtMalloc(reinterpret_cast<void**>(&deviceWorkspace),
                          sizeWorkspace, ACL_MEM_MALLOC_HUGE_FIRST));
}
matmulOp.Initialize(arguments, deviceWorkspace);          // 指针传入（即便为 nullptr）
matmulOp(stream, aicCoreNum);
ACL_CHECK(aclrtSynchronizeStream(stream));
if (sizeWorkspace > 0) {                                  // 仅当申请过才释放
    ACL_CHECK(aclrtFree(deviceWorkspace));
}
```

注意两个细节：

- `Initialize(arguments, deviceWorkspace)` 始终传指针，即便 `deviceWorkspace == nullptr`（`BasicMatmul` 会忽略它）。
- 释放放在 `aclrtSynchronizeStream` **之后**——必须等 kernel 真正用完 workspace 才能释放，否则会踩到正在使用的显存。这和 u2-l1 讲的「释放与申请逆序、且在同步之后」是一致的。

#### 4.3.4 代码实践

**实践目标**：对比 `BasicMatmul` 与 `SplitkMatmul` 的 workspace 处理，亲手验证「基础 GEMM 不申请、SplitK 才申请」。

**操作步骤**：

1. 打开 [include/catlass/gemm/kernel/splitk_matmul.hpp:202-248](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/splitk_matmul.hpp#L202-L248) 的 `GetSplitkFactor`，阅读它如何根据 K 大小（≤1024/2048/4096/更大）限幅 `maxSplitkFactor`（2/4/8/16）。
2. 假设 `M=256, N=512, K=4096`，AIC 核数 20，`workspaceElementSize=4`（fp32）：手动套用 `GetWorkspaceSize` 公式估算一个数量级（先假设 `splitkFactor=8`，结果约 \[ 4 \times 256 \times 512 \times 8 = 4\,194\,304 \text{ 字节} \approx 4\text{ MB} \]）。
3. 思考：为什么 `basic_matmul.cpp` 里 `if (sizeWorkspace > 0)` 守卫对基础 GEMM 永远走 false 分支？

**预期结果**：你会确认 `BasicMatmul::GetWorkspaceSize` 恒返回 0，所以 `deviceWorkspace` 保持 `nullptr`，既不申请也不释放；而换成 SplitK 样例（如 `09_splitk_matmul`）后，同样的 Host 模板会正确申请出几 MB 的 workspace。

> 待本地验证：把 `09_splitk_matmul` 的 Host 代码与 `00_basic_matmul` 对比，确认它也是同一套 `GetWorkspaceSize → if(>0) malloc → Initialize → operator() → sync → if(>0) free` 模板，只是 `Arguments` 里多塞了 `aicCoreNum` 与 `workspaceElementSize`。

#### 4.3.5 小练习与答案

**练习 1**：如果 Host 忘记在 `aclrtSynchronizeStream` 之前就 `aclrtFree(deviceWorkspace)`，会发生什么？

> **答案**：kernel 可能仍在异步读写 workspace，提前释放会导致 kernel 访问已释放显存，结果错误甚至崩溃。释放必须在同步之后。

**练习 2**：`SplitkMatmul` 的 `Arguments` 比 `BasicMatmul` 多了 `aicCoreNum` 和 `workspaceElementSize`，但 `DeviceGemm` 的调用流程完全没变。这说明了 `DeviceGemm` 适配器设计的什么优点？

> **答案**：说明 `DeviceGemm` 把 Host 调用约定（四接口 + 顺序）与具体 Kernel 的 `Arguments` 字段解耦。Kernel 可以自由扩展 `Arguments` 内容，只要维持 `CanImplement/GetWorkspaceSize/ToUnderlyingArguments` 三件套签名，Host 侧 `DeviceGemm` 的调用代码就不必改动。

---

## 5. 综合实践

本讲实践任务（来自讲义规格）：**画出 `Arguments → Params → Stream 执行` 的完整调用时序，并说明 `GetWorkspaceSize` 返回非 0 时 Host 该如何处理。**

请按以下步骤完成：

1. **画时序图**。横向为时间轴，纵向分三条泳道：`Host（basic_matmul.cpp）`、`DeviceGemm`、`Kernel（BasicMatmul）`。画出从 `arguments` 构造开始，经过 `CanImplement → GetWorkspaceSize → Initialize（此时发生 Arguments→Params 转换，写 params_）→ operator() → Run() → KernelAdapter<<<>>> 派发 → aclrtSynchronizeStream` 的完整消息流。标注清楚：哪些步骤在 Host CPU 执行、哪个步骤把 `params_` 拷进 NPU。

2. **处理非零 workspace**。基于 4.3 的 SplitK 例子，写出 Host 侧伪代码（不要照抄，自己重写一遍），体现：
   - 用 `GetWorkspaceSize` 取大小；
   - 用 `if (size > 0)` 守卫 `aclrtMalloc`；
   - 把指针传给 `Initialize`；
   - 同步之后再 `if (size > 0)` 守卫 `aclrtFree`。

3. **回答关键问题**：如果 `GetWorkspaceSize` 返回非 0，但 Host 偷懒不申请、直接传 `nullptr` 给 `Initialize`，对 `BasicMatmul` 和 `SplitkMatmul` 分别会有什么后果？

   > 参考答案：对 `BasicMatmul` 无影响（它忽略 workspace，本就返回 0）；对 `SplitkMatmul` 则会致命——`Params.ptrWorkspace` 变成空指针，设备侧 AIC 把部分和写入 `nullptr`、AIV 再从空地址 ReduceAdd，必然段错误或写出垃圾数据。这正说明「`GetWorkspaceSize` 返回值是 Host 申请的契约，必须遵守」。

4. **进阶（可选）**：打开 [include/catlass/detail/kernel_adapter.hpp:18-31](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/detail/kernel_adapter.hpp#L18-L31)，确认 `operator()` 派发的最终落点是 `Operator op; op(params);`——即构造 Kernel 对象并调用其 `operator()(Params)`。把这步补进你的时序图，使链条从 Host 一直连到设备侧 SPMD 主循环入口（下一讲 u2-l4 的主题）。

## 6. 本讲小结

- `DeviceGemm` 是 Device 层的**薄适配器**：只持有一个 `Params params_` 成员，四个接口 `CanImplement/GetWorkspaceSize/Initialize/operator()` 几乎都在**转发**给 Kernel，自身无业务逻辑。
- 前三个接口负责「准备」：能力检查、算 workspace 大小、把 `Arguments` 转成 `Params`；`operator()` 负责「执行」：用 `KernelAdapter<<<blockDim>>>` 把 `params_` 派发到 NPU。
- **Arguments vs Params** 是用户视图与内核视图的分离：用户只填 `problemShape` + 指针，Kernel 在 `ToUnderlyingArguments` 里补上 Layout（及 splitk 的 workspace/splitkFactor）。统一签名让 `DeviceGemm` 能用同一段代码驱动所有 Kernel。
- **Workspace** 走「算大小 → 按需申请 → 传入 Initialize → 同步后释放」四步；`BasicMatmul` 恒为 0，`SplitkMatmul` 非零（\(\text{size} = \text{elemSize} \times M \times N \times \text{splitkFactor}\)）。
- 派发用 `<<<blockDim, nullptr, stream>>>` 这一 AscendC 语法，`blockDim` 即 AIC 核数；`CATLASS_ARCH` 分支封装了 2201/3510 的同步差异，Host 无感。
- Host 调用是**异步**的，必须 `aclrtSynchronizeStream` 才算完成；workspace 的释放必须排在同步之后。

## 7. 下一步学习建议

本讲把链条追到了「`operator()` 把 `params_` 派发进 NPU」，但还没进入 Kernel 在设备侧具体怎么算。下一讲 **u2-l4 Kernel 层 BasicMatmul 与 SPMD 循环** 会拆开 `basic_matmul.hpp` 的 `operator()<AIC>`，讲清：

- `BlockScheduler` 如何用 `GetCoreLoops/GetBlockCoord` 把 C 矩阵基本块分给各 AICore；
- SPMD 步长循环 `for(loopIdx=GetBlockIdx(); ...; loopIdx+=GetBlockNum())` 的分核逻辑；
- `gmOffsetA/B/C` 如何由 `params.layout.GetOffset` 算出，再喂给 `blockMmad`。

建议同时泛读：

- [include/catlass/gemm/kernel/basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp) 的 `operator()<AIC>` 全文，提前感受 SPMD 循环结构；
- U3 的 Layout（u3-l1）与 GemmType/TileShape（u3-l2），理解 `params.layoutA.GetOffset` 背后的偏移计算原理。
