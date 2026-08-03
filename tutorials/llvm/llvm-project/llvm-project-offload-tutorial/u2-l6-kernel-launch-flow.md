# target 内核启动流程

## 1. 本讲目标

本讲沿着 OpenMP `target` 区域的**执行链路**自顶向下走读源码，回答三个问题：

1. 当源码里写下一句 `#pragma omp target` 时，Clang 生成的 `__tgt_target_kernel` 调用，最终是如何把一个设备内核真正"启动"起来的？
2. 编译器交给运行时的那个大结构 `KernelArgsTy`，里面每个字段分别从哪里来、又分别流向了哪一步？
3. 内核启动与上一讲（u2-l5）的"数据搬运"在什么时机衔接？

学完后你应该能够：

- 画出从 `__tgt_target_kernel` 到设备插件 `launch_kernel` 的完整调用链。
- 说清 `KernelArgsTy` / `KernelExtraArgsTy` 的来源、用途与 ABI 版本机制。
- 解释"数据映射在内核启动前后如何被复用"的衔接关系。

## 2. 前置知识

本讲默认你已经掌握前置讲义的结论，这里只做最简回顾：

- **u1-l5**：Clang 与运行时之间靠 C ABI 解耦；`KernelArgsTy` 首字段是 `Version`，用 `static_assert` 锁死结构布局以保证 ABI 稳定。`tgt_map_type` 把 `map` 子句压成每变量一个 `uint64_t` 位掩码。
- **u2-l2 / u2-l3**：全局单例 `PM`（`PluginManager`）管理所有设备；`DeviceTy` 是"上层 OpenMP 逻辑"与"底层插件"之间的外观（Facade），自身不碰硬件，只把请求翻译成 `RTL->某方法(RTLDeviceID, ...)`。
- **u2-l5**：`targetDataBegin` / `targetDataEnd` / `targetDataUpdate` 三阶段共用 `targetData<>` 模板外壳；"搬运"与"引用计数"是正交的两件事。

两个本讲会反复用到的术语：

- **HostPtr（主机入口指针）**：Clang 为每个 `target` 区域生成一个"主机侧桩函数"，其地址（`HostPtr`）是定位对应设备内核的**唯一标识**。运行时拿到它，去翻译表里查"它在设备镜像里对应第几号入口"。
- **AsyncInfo（异步上下文）**：本次 `target` 区域里所有设备操作（数据搬运、内核启动）挂载到的那个"队列"。`BLOCKING` 模式下末尾阻塞同步；`NON_BLOCKING` 模式下只轮询查询。详见 u2-l7。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [libomptarget/interface.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp) | 编译器入口 `__tgt_target_kernel` 与 `targetKernel` 模板的实现，是本讲的"上层门"。 |
| [libomptarget/omptarget.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp) | `target()` 主流程，以及它调用的 `processDataBefore` / `processDataAfter`，是本讲的"中段"。 |
| [include/Shared/APITypes.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h) | `KernelArgsTy` / `KernelExtraArgsTy` / `__tgt_async_info` 的定义，是编译器与运行时共享的 ABI 类型。 |
| [include/device.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h) 与 [libomptarget/device.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp) | `DeviceTy::launchKernel` 的声明与实现，把上层请求转发给插件。 |
| [include/rtl.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/rtl.h) | `TranslationTable` / `TableMap` 结构，描述"主机入口 → 设备入口表"的查找账本。 |
| [plugins-nextgen/common/src/PluginInterface.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp) | `GenericPluginTy::launch_kernel`，是本讲的"出口"（与 u3-l1 衔接）。 |

---

## 4. 核心概念与源码讲解

### 4.1 入口与分发：`__tgt_target_kernel` → `targetKernel`

#### 4.1.1 概念说明

当你写下：

```cpp
#pragma omp target map(tofrom: a[0:N])
{
  // 内核体
}
```

Clang 不会真的"翻译"这段代码，它只是为内核体生成一段**设备镜像**（编译期已完成，见 u1-l1/u1-l5），并在主机侧生成一次对运行时的调用：

```cpp
__tgt_target_kernel(loc, device_id, num_teams, thread_limit,
                    /*HostPtr=*/&__omp_offloading_..., &KernelArgs);
```

这就是"编译器-运行时契约"里**内核启动类**的入口。运行时的职责只有三件：

1. 校验设备、把 `KernelArgs` 升级成运行时内部统一格式；
2. 在内核启动前搬运数据、启动后搬回数据；
3. 把"启动"这件事交给对应设备的插件。

注意"是否带 `nowait`"在这里就已经分叉了——它决定本次用的是阻塞型 `AsyncInfoTy`，还是挂在 OpenMP task 上的 `TaskAsyncInfoWrapperTy`（见 u2-l7）。

#### 4.1.2 核心流程

```
__tgt_target_kernel(Loc, DeviceId, NumTeams, ThreadLimit, HostPtr, KernelArgs)
        │
        │  若 KernelArgs.Flags.NoWait 为真 → 用 TaskAsyncInfoWrapperTy
        │  否则                           → 用 AsyncInfoTy（阻塞）
        ▼
targetKernel<TargetAsyncInfoTy>(...)        ← 模板，参数化"异步上下文类型"
        │
        ├── checkDevice(DeviceId, Loc)      ← 决定是否真的卸载（可能直接回主机）
        ├── upgradeKernelArgs(...)          ← 把旧版 KernelArgs 升级为内部统一格式
        ├── 构造 TargetAsyncInfoTy(Device)
        ├── target(Loc, Device, HostPtr, *KernelArgs, AsyncInfo)   ← 真正的主流程
        ├── AsyncInfo.synchronize()         ← 等待本次所有设备操作完成
        └── handleTargetOutcome(...)        ← 按 OffloadPolicy 处理成败
```

#### 4.1.3 源码精读

入口函数本身极短，唯一逻辑是按 `nowait` 分发：

[libomptarget/interface.cpp:462-471](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L462-L471) —— `__tgt_target_kernel`：根据 `NoWait` 标志选择异步上下文类型，其余参数原样转发给 `targetKernel` 模板。

```cpp
EXTERN int __tgt_target_kernel(ident_t *Loc, int64_t DeviceId, int32_t NumTeams,
                               int32_t ThreadLimit, void *HostPtr,
                               KernelArgsTy *KernelArgs) {
  OMPT_IF_BUILT(ReturnAddressSetterRAII RA(__builtin_return_address(0));)
  if (KernelArgs->Flags.NoWait)
    return targetKernel<TaskAsyncInfoWrapperTy>(
        Loc, DeviceId, NumTeams, ThreadLimit, HostPtr, KernelArgs);
  return targetKernel<AsyncInfoTy>(Loc, DeviceId, NumTeams, ThreadLimit,
                                   HostPtr, KernelArgs);
}
```

> 说明：`__tgt_target_kernel` 的 C 签名声明在 [include/omptarget.h:415](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L415)。`OMPT_IF_BUILT` 是仅在编译期开启 OMPT 时才展开的宏，用于记录返回地址供工具回调。

`checkDevice` 决定"这次到底要不要真的卸载"。它的返回值含义很关键：

[libomptarget/interface.cpp:52-76](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L52-L76) —— `checkDevice`：当卸载被禁用、目标设备是主机、或没有可用设备时返回 `true`（表示"别卸载了"），并把 `DeviceID == OFFLOAD_DEVICE_DEFAULT` 解析成默认设备号。

```cpp
bool checkDevice(int64_t &DeviceID, ident_t *Loc) {
  if (OffloadPolicy::get(*PM).Kind == OffloadPolicy::DISABLED) { ... return true; }
  if (DeviceID == OFFLOAD_DEVICE_DEFAULT)
    DeviceID = omp_get_default_device();
  if (omp_get_num_devices() == 0) { handleTargetOutcome(false, Loc); return true; }
  if (isInitialDevice(static_cast<int>(DeviceID))) { ... return true; }
  return false; // false = 目标设备就绪，可以卸载
}
```

> 注意：`checkDevice` 返回 `true` 时 `targetKernel` 直接 `return OMP_TGT_FAIL`，即"本次不卸载"——这并不一定是错误，而是 `if(target)` / 设备不存在等场景的预期回退。

随后 `targetKernel` 构造异步上下文、调用 `target()`、再同步：

[libomptarget/interface.cpp:427-448](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L427-L448) —— 取设备、构造 `TargetAsyncInfoTy`、调用 `target()`、同步并处理结果。

```cpp
  auto DeviceOrErr = PM->getDevice(DeviceId);
  if (!DeviceOrErr)
    FATAL_MESSAGE(DeviceId, "%s", toString(DeviceOrErr.takeError()).c_str());

  TargetAsyncInfoTy TargetAsyncInfo(*DeviceOrErr);
  AsyncInfoTy &AsyncInfo = TargetAsyncInfo;
  ...
  int Rc = OFFLOAD_SUCCESS;
  Rc = target(Loc, *DeviceOrErr, HostPtr, *KernelArgs, AsyncInfo);
  {
    TIMESCOPE_WITH_DETAILS_AND_IDENT("Runtime: synchronize", "", Loc);
    if (Rc == OFFLOAD_SUCCESS)
      Rc = AsyncInfo.synchronize();
    handleTargetOutcome(Rc == OFFLOAD_SUCCESS, Loc);
  }
  return OMP_TGT_SUCCESS;
```

> 说明：`TargetAsyncInfoTy` 是模板参数（`AsyncInfoTy` 或 `TaskAsyncInfoWrapperTy`），二者都可隐式转换为 `AsyncInfoTy&`（有 `static_assert` 保障），因此 `target()` 只看到统一的 `AsyncInfoTy`。同步 `synchronize()` 之后才调用 `handleTargetOutcome`——这意味着**内核真正执行完成的判定点在 `targetKernel` 这里**，而不是 `target()` 内部。

#### 4.1.4 代码实践

**目标**：确认 `__tgt_target_kernel` 的调用与参数来源。

1. 写一个最小 `target` 程序，用 `clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu -S -emit-llvm` 生成 LLVM IR。
2. 在 IR 里搜索 `__tgt_target_kernel`，观察它的 6 个实参分别是什么（`loc`、`device_id`、`num_teams`、`thread_limit`、`HostPtr`、`KernelArgs`）。
3. 对比带 `nowait` 与不带的版本，观察生成的调用是否有差异（注意：`nowait` 在这一层主要影响运行时内部异步类型，IR 侧调用名可能相同）。

**需要观察的现象**：`HostPtr` 是一个形如 `@__omp_offloading_<...>_l<行号>` 的主机桩函数地址；`KernelArgs` 指向一个全局或栈上的 `KernelArgsTy` 结构。

**预期结果**：你能逐字段对应出 `NumArgs`、`ArgPtrs`、`ArgSizes`、`ArgTypes` 等就是 `map` 子句展开后的数组。

> 若没有可用的 clang 工具链，本步可作为"源码阅读型实践"，直接阅读 Clang 的 OpenMP codegen 亦可，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`checkDevice` 何时会返回 `true`？返回 `true` 后 `targetKernel` 的行为是什么？

> **答案**：当卸载策略为 `DISABLED`、目标设备是初始设备（主机）、或 `omp_get_num_devices()==0` 时返回 `true`。此时 `targetKernel` 直接 `return OMP_TGT_FAIL`，不进入 `target()`。

**练习 2**：为什么 `targetKernel` 是模板、而不是普通函数？

> **答案**：为了在不引入运行时分支的前提下，让阻塞型（`AsyncInfoTy`）与 `nowait` 型（`TaskAsyncInfoWrapperTy`）走各自的异步上下文构造与同步语义。模板在编译期就实例化出两条代码路径。

---

### 4.2 参数升级：`KernelArgsTy` 的 `Version` 与 `upgradeKernelArgs`

#### 4.2.1 概念说明

`KernelArgsTy` 是编译器和运行时共享的"内核启动参数包"。因为编译器版本和运行时版本可能不完全对齐，结构里特意放了一个 `Version` 字段做 **ABI 版本协商**：

- 不同版本的编译器可能填了**不同数量**的字段（早期版本没有 `DynCGroupMem`、`UserNumBlocks` 等）。
- 运行时通过 `Version` 判断"编译器到底填到第几格"，并在进入主流程前把它**升级补全**成内部统一格式。

此外，引入"动态指针槽（dyn_ptr）"后的版本还会在参数末尾隐式追加一个特殊的 `TARGET_PARAM | LITERAL` 参数。`upgradeKernelArgs` 负责把这一差异抹平。

#### 4.2.2 核心流程

```
KernelArgs->Version
   │
   ├── Version < MIN_VERSION_WITH_DYN_PTR  → 重建到 LocalKernelArgs，补默认值
   ├── Version == MIN_VERSION_WITH_DYN_PTR → 末尾追加 1 个 dyn_ptr 隐式参数
   └── Version == OMP_KERNEL_ARG_VERSION   → 直接返回，仅做一点多维校正
```

#### 4.2.3 源码精读

先看参数包本身：

[include/Shared/APITypes.h:91-124](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L91-L124) —— `KernelArgsTy`：内核启动的全部参数。关键字段如下。

```cpp
struct KernelArgsTy {
  uint32_t Version = 0;       // ABI 版本，用于协商结构布局
  uint32_t NumArgs = 0;       // map 子句展开后的参数个数
  void **ArgBasePtrs;         // 每个参数的基指针（如结构体基址）
  void **ArgPtrs;             // 每个参数的数据指针
  int64_t *ArgSizes;          // 每个参数的字节数
  int64_t *ArgTypes;          // 每个参数的 tgt_map_type 位掩码
  void **ArgNames;            // 调试用名字，可空
  void **ArgMappers;          // 用户自定义 mapper，可空
  uint64_t Tripcount;         // teams/distribute 循环的 tripcount，否则 0
  struct { uint64_t NoWait:1; uint64_t IsCUDA:1; ... } Flags;
  uint32_t UserNumBlocks[3];  // 用户请求的 block 数（x,y,z）
  uint32_t UserThreadLimit[3];// 用户请求的线程数（x,y,z）
  uint32_t DynCGroupMem;      // 请求的动态 cgroup（共享）内存
};
static_assert(sizeof(KernelArgsTy) == (...), "Invalid struct size");
```

> 说明：`ArgBasePtrs/ArgPtrs/ArgSizes/ArgTypes/ArgNames/ArgMappers` 这 6 个数组**同长 `NumArgs`**，按下标一一对应——它们就是 `map` 子句展开后的每变量描述（与 u2-l5 完全一致）。`static_assert` 锁死总大小，保证跨编译器/运行时版本的二进制兼容。

`KernelExtraArgsTy` 是运行时内部附加的、不由编译器填充的参数：

[include/Shared/APITypes.h:150-152](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L150-L152) —— `KernelExtraArgsTy`：目前只携带录制重放的输出回执 `ReplayOutcome`（见 u3-l8）。

```cpp
struct KernelExtraArgsTy {
  KernelReplayOutcomeTy *ReplayOutcome = nullptr;
};
```

再看升级逻辑：

[libomptarget/interface.cpp:289-368](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L289-L368) —— `upgradeKernelArgs`：按 `Version` 把旧结构重建到局部 `LocalKernelArgs`，必要时在末尾追加 dyn_ptr 隐式参数。

关键分支（节选）：

```cpp
// Versions before OMP_KERNEL_ARG_MIN_VERSION_WITH_DYN_PTR 缺少若干字段，重建完整结构
if (KernelArgs->Version < OMP_KERNEL_ARG_MIN_VERSION_WITH_DYN_PTR) {
  LocalKernelArgs.Version = KernelArgs->Version;
  LocalKernelArgs.NumArgs = KernelArgs->NumArgs;
  // ... 复制各数组指针 ...
  LocalKernelArgs.UserNumBlocks[0] = NumTeams;   // 用入参补默认
  LocalKernelArgs.UserThreadLimit[0] = ThreadLimit;
  return &LocalKernelArgs;
}

// Version == MIN_VERSION_WITH_DYN_PTR：末尾隐式追加 dyn_ptr 参数
if (KernelArgs->Version == OMP_KERNEL_ARG_MIN_VERSION_WITH_DYN_PTR) {
  uint32_t NewSize = KernelArgs->NumArgs + 1;
  // ... 把原数组拷进 Bufs，再在第 NewSize-1 位放一个 TARGET_PARAM|LITERAL ...
  LocalKernelArgs.NumArgs = NewSize;
  return &LocalKernelArgs;
}
return KernelArgs; // 当前版本，原样返回
```

> 说明：`OMP_KERNEL_ARG_VERSION` 与 `OMP_KERNEL_ARG_MIN_VERSION_WITH_DYN_PTR` 定义于 LLVM OpenMP 前端/运行时共享的 ABI 头文件（属于编译器-运行时契约的一部分，不在 `offload/` 目录内）。它们的具体数值会随版本演进，但语义固定：前者是"当前最新版本"，后者是"引入隐式 dyn_ptr 槽的最早版本"。`targetKernel` 在调用 `target()` 前先 `KernelArgs = upgradeKernelArgs(...)`，因此主流程拿到的永远是补全后的统一格式。

#### 4.2.4 代码实践

**目标**：理解"末尾隐式参数"在后续被如何对待。

1. 阅读 `targetKernel` 里 [interface.cpp:402-405](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L402-L405)：`UserArgCount` 为何要从 `NumArgs` 减 1。
2. 思考：这个被减掉的"第 0 号隐式 dyn_ptr"参数，在 `processDataBefore` 里会落入哪个分支？

**需要观察的现象**：该隐式参数的 `ArgType` 是 `OMP_TGT_MAPTYPE_TARGET_PARAM | OMP_TGT_MAPTYPE_LITERAL`。

**预期结果**：它在 `processDataBefore` 中命中 `LITERAL` 分支，被当作"按值转发的 firstprivate 值"直接传给内核，不参与映射。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `KernelArgsTy` 的首字段必须是 `Version`，且结构大小被 `static_assert` 锁死？

> **答案**：编译器和运行时可能来自不同构建版本，需要靠首字段协商布局。`static_assert` 保证指针/数组字段的总大小在当前版本下是确定的，避免因编译器/运行时结构不一致导致越界读字段。

**练习 2**：`KernelExtraArgsTy` 与 `KernelArgsTy` 的区别是什么？

> **答案**：`KernelArgsTy` 由编译器填充，描述用户面参数；`KernelExtraArgsTy` 由运行时内部使用，携带编译器不感知的附加信息（目前仅 `ReplayOutcome`）。普通启动时它传 `nullptr`。

---

### 4.3 `target()` 主流程：从 HostPtr 定位内核到启动

#### 4.3.1 概念说明

`target()` 是整条链路的"心脏"。它解决一个核心问题：**给定一个主机桩函数地址 `HostPtr`，如何在设备上找到对应内核并启动它？**

答案是靠"翻译表"：

- 注册期（u2-l1）每个库（`__tgt_bin_desc`）都被登记进一张 `TranslationTable`，记录"主机入口表"与"每个设备号对应的已加载入口表（`TargetsTable`）"。
- `HostPtr` 先在 `HostPtrToTableMap` 缓存里查（命中即免搜索）；找不到再线性扫所有库。
- 命中后得到一个 `TableMap{Table, Index}`：`Table` 指向某库的翻译表，`Index` 是该 `HostPtr` 在主机入口表里的下标。
- 用 `Table->TargetsTable[DeviceId]` 取出"该设备已加载的入口视图 `__tgt_target_table`"，再取 `TargetTable->EntriesBegin[Index].Address`——这就是**设备上的内核入口地址**。

整个过程本质是两步查找：

\[ \text{HostPtr} \xrightarrow{\text{TableMap}} (\text{TranslationTable}, \text{Index}) \xrightarrow{\text{DeviceId}} \text{设备入口地址} \]

#### 4.3.2 核心流程

```
target(Loc, Device, HostPtr, KernelArgs, AsyncInfo)
   │
   ├── TM = getTableMap(HostPtr)         ← 主机入口 → 翻译表项
   ├── TargetTable = TM->Table->TargetsTable[DeviceId]   ← 取该设备的入口视图
   ├── 若 NumArgs>0：
   │     ├── processDataBefore(...)       ← 建映射 + 搬数据 + 组装 TgtArgs/TgtOffsets
   │     └── KernelArgs.NumArgs = TgtArgs.size()   ← 用"设备侧参数个数"覆盖
   ├── TgtEntryPtr = TargetTable->EntriesBegin[TM->Index].Address  ← 设备内核地址
   ├── Device.launchKernel(TgtEntryPtr, TgtArgs, TgtOffsets, KernelArgs, ...)
   └── 若 NumArgs>0：processDataAfter(...)   ← 搬回数据 + 释放私有参数内存
```

#### 4.3.3 源码精读

先看两张表的定义：

[include/rtl.h:27-53](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/rtl.h#L27-L53) —— `TranslationTable` 与 `TableMap`：翻译表记录"每设备号的已加载入口视图 `TargetsTable`"，`TableMap` 把 `HostPtr` 映射到 `(TranslationTable*, Index)`。

```cpp
struct TranslationTable {
  __tgt_target_table HostTable;                       // 主机侧入口表
  llvm::SmallVector<__tgt_target_table *> TargetsTable; // 每设备号一份：NULL=未加载
  // ... 还有 TargetsImages / TargetsEntries ...
};
struct TableMap {
  TranslationTable *Table = nullptr;  // 该 HostPtr 所属的翻译表
  uint32_t Index = 0;                 // HostPtr 在入口表里的下标
};
```

`getTableMap` 实现"先查缓存、未命中再扫库"，并把结果回填缓存：

[libomptarget/omptarget.cpp:1621-1653](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1621-L1653) —— `getTableMap`：先在 `HostPtrToTableMap` 里查，未命中则锁住翻译表线性扫描，找到后回填缓存以加速下次查找。

```cpp
TableMap *getTableMap(void *HostPtr) {
  std::lock_guard<std::mutex> TblMapLock(PM->TblMapMtx);
  auto It = PM->HostPtrToTableMap.find(HostPtr);
  if (It != PM->HostPtrToTableMap.end()) return &It->second;  // 缓存命中
  // 未命中：扫所有已注册库
  std::lock_guard<std::mutex> TrlTblLock(PM->TrlTblMtx);
  for (auto &... : PM->HostEntriesBeginToTransTable) {
    // 在该库主机入口表里逐条比对 Address == HostPtr
    // 命中则回填 HostPtrToTableMap 并返回
  }
  return nullptr;
}
```

> 说明：这里体现了 u2-l2 讲过的锁序——外层 `TblMapMtx`、内层 `TrlTblMtx`。`HostPtrToTableMap` 是"热路径缓存"，绝大多数调用第一次之后都命中。

`target()` 主流程：

[libomptarget/omptarget.cpp:2286-2306](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2286-L2306) —— `target()` 开头：定位翻译表项并取出该设备的入口视图。

```cpp
int target(ident_t *Loc, DeviceTy &Device, void *HostPtr,
           KernelArgsTy &KernelArgs, AsyncInfoTy &AsyncInfo) {
  int32_t DeviceId = Device.DeviceID;
  TableMap *TM = getTableMap(HostPtr);
  if (!TM) { REPORT() << "Host ptr ... does not have a matching target pointer."; return OFFLOAD_FAIL; }

  __tgt_target_table *TargetTable = nullptr;
  {
    std::lock_guard<std::mutex> TrlTblLock(PM->TrlTblMtx);
    TargetTable = TM->Table->TargetsTable[DeviceId];  // 该设备的已加载入口视图
  }
  assert(TargetTable && "Global data has not been mapped");
```

> 说明：若 `TargetsTable[DeviceId]` 为 `NULL`，说明该镜像尚未在该设备上加载——这通常发生在镜像延迟加载（u2-l2 的 `loadImagesOntoDevice`）尚未触发时；正常 `getDevice()` 路径已确保加载完成。

随后准备参数、启动、收尾：

[libomptarget/omptarget.cpp:2344-2392](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2344-L2392) —— 取设备内核地址、启动内核、收尾。

```cpp
  void *TgtEntryPtr = TargetTable->EntriesBegin[TM->Index].Address;  // 设备内核地址
  {
    assert(KernelArgs.NumArgs == TgtArgs.size() && "Argument count mismatch!");
    TIMESCOPE_WITH_DETAILS_AND_IDENT("Kernel Target", ..., Loc);
#ifdef OMPT_SUPPORT
    InterfaceRAII TargetSubmitRAII(
        RegionInterface.getCallbacks<ompt_callback_target_submit>(), NumTeams);
#endif
    Ret = Device.launchKernel(TgtEntryPtr, TgtArgs.data(), TgtOffsets.data(),
                              KernelArgs, nullptr, AsyncInfo);
  }
  ...
  if (NumClangLaunchArgs)
    Ret = processDataAfter(Loc, DeviceId, HostPtr, NumClangLaunchArgs, ...);
```

> 说明：`InterfaceRAII` 是 OMPT 工具回调锚点（派发 `ompt_callback_target_submit` before/after，详见 u3-l10），未开 OMPT 时编译消除。`launchKernel` 的第 5 个参数 `nullptr` 即 `KernelExtraArgsTy*`，普通启动不携带；`target_replay` 路径才会传非空（见 [omptarget.cpp:2512](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2512)）。

#### 4.3.4 代码实践

**目标**：亲手走一遍"HostPtr → 设备入口地址"的两步查找。

1. 在 [omptarget.cpp:2289](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2289) 的 `getTableMap(HostPtr)` 处，对照 [getTableMap 实现](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1621-L1653)，画出数据流。
2. 注意 `TM->Index` 这个下标在两处被使用：一次取 `TargetsTable[DeviceId]`（用 `DeviceId`），一次取 `EntriesBegin[TM->Index]`（用 `Index`）。确认这两个下标语义不同。

**需要观察的现象**：`DeviceId` 用来在"同一库里挑设备视图"，`Index` 用来在"同一视图里挑内核条目"。

**预期结果**：你能用一句话说清 `TgtEntryPtr = TargetTable->EntriesBegin[TM->Index].Address` 这一行的含义——"在本设备已加载的入口表里，取第 `Index` 号条目的地址，即设备内核入口"。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `getTableMap` 要先查 `HostPtrToTableMap` 缓存，而不是直接扫库？

> **答案**：扫库是 O(库数 × 每库入口数) 的线性扫描，且要加 `TrlTblMtx` 锁。`HostPtrToTableMap` 是 O(log n) 的 `std::map` 查找且只加更轻的 `TblMapMtx`。同一 `HostPtr` 会被反复启动，缓存把热路径降为近似 O(1)。

**练习 2**：如果 `TargetsTable[DeviceId]` 为 `nullptr` 会发生什么？

> **答案**：`target()` 里的 `assert(TargetTable)` 会触发（调试构建），表明镜像未在该设备加载。正常运行时 `PM->getDevice(DeviceId)` 已触发延迟加载，不会走到这里。

---

### 4.4 启动前后衔接数据映射：`processDataBefore` / `processDataAfter`

#### 4.4.1 概念说明

内核启动不是孤立事件：内核要用到的变量必须先在设备上"就位"（建映射 + 搬过去），内核跑完后又要把结果搬回、释放临时内存。本讲最关键的衔接点是：

> **`target()` 直接复用了 u2-l5 的 `targetDataBegin` / `targetDataEnd`**，只是把它们包进了两个辅助函数 `processDataBefore` / `processDataAfter`。

也就是说，`target` 区域对每个 `map` 变量的处理，和独立的 `target data` 区域**完全一致**——这是运行时有意的复用设计。差异只在于：`target` 区域还需要把每个变量翻译成"传给内核的实参（设备指针 + 偏移）"，并管理私有变量（private/firstprivate）。

这里出现一个本讲专有的数据结构 `PrivateArgumentManagerTy`：它把若干小的 firstprivate 参数**打包成一次传输**，以减少 H2D 拷贝次数。

#### 4.4.2 核心流程

```
processDataBefore:
   targetDataBegin(...)              ← 复用 u2-l5：建映射 + H2D（to/tofrom）
   processAttachEntries(...)         ← 处理延迟的 ATTACH
   遍历每个参数，按 ArgType 分流：
     LITERAL        → 直接当值传（TgtArgs.push_back(HstPtrBase)）
     PRIVATE        → 交给 PrivateArgumentManager（可能打包）
     TARGET_PARAM   → getTgtPtrBegin 查设备指针，记录 (TgtPtrBegin, 偏移)
   PrivateArgumentManager.packAndTransfer()  ← 打包并一次性传输 firstprivate
processDataAfter:
   targetDataEnd(...)                ← 复用 u2-l5：D2H（from）+ 引用计数递减
   注册 post-processing：释放私有参数设备内存（在同步后执行）
```

#### 4.4.3 源码精读

`processDataBefore` 调用 `targetDataBegin` 复用映射逻辑：

[libomptarget/omptarget.cpp:2055-2078](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2055-L2078) —— `processDataBefore` 开头：构造 `StateInfo`，调用 `targetDataBegin` 建映射与搬运，再处理 ATTACH。

```cpp
static int processDataBefore(ident_t *Loc, int64_t DeviceId, void *HostPtr,
                             int32_t ArgNum, void **ArgBases, ...) {
  auto DeviceOrErr = PM->getDevice(DeviceId);
  StateInfoTy StateInfo;
  int Ret = targetDataBegin(Loc, *DeviceOrErr, ArgNum, ArgBases, Args, ArgSizes,
                            ArgTypes, ArgNames, ArgMappers, AsyncInfo,
                            &StateInfo, false /*FromMapper=*/);
  if (Ret != OFFLOAD_SUCCESS) { ... return OFFLOAD_FAIL; }
  if (!StateInfo.AttachEntries.empty())
    Ret = processAttachEntries(*DeviceOrErr, StateInfo, AsyncInfo);
```

> 说明：这里的 `targetDataBegin` 与 u2-l5 讲的是**同一个函数**——`target` 区域的 `map` 子句和 `target data` 的 `map` 子句在运行时被一视同仁。`StateInfoTy` 是本次构造的临时账本（记录新分配、ATTACH、跳过的 FROM），仅 begin/end 需要。

随后按参数类型组装传给内核的实参：

[libomptarget/omptarget.cpp:2092-2220](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2092-L2220) —— 遍历参数分流：`LITERAL` 直接传值，`PRIVATE` 入私有管理器，`TARGET_PARAM` 查设备指针并记录偏移。

核心分支（节选）：

```cpp
for (int32_t I = 0; I < ArgNum; ++I) {
  if (!(ArgTypes[I] & OMP_TGT_MAPTYPE_TARGET_PARAM)) {
    // 非 target 参数：处理 lambda 映射等，不入 TgtArgs
    ...
    continue;
  }
  if (ArgTypes[I] & OMP_TGT_MAPTYPE_LITERAL) {          // 按值转发
    TgtPtrBegin = HstPtrBase; TgtBaseOffset = 0;
  } else if (ArgTypes[I] & OMP_TGT_MAPTYPE_PRIVATE) {   // 私有/firstprivate
    Ret = PrivateArgumentManager.addArg(HstPtrBegin, ArgSizes[I], ...);
  } else {                                              // 普通 target 参数
    TPR = DeviceOrErr->getMappingInfo().getTgtPtrBegin(HstPtrBegin, ArgSizes[I],
                                                       /*UpdateRefCount=*/false, ...);
    TgtPtrBegin = TPR.TargetPointer;
    TgtBaseOffset = (intptr_t)HstPtrBase - (intptr_t)HstPtrBegin;
  }
  TgtArgsPositions[I] = TgtArgs.size();
  TgtArgs.push_back(TgtPtrBegin);       // 设备指针
  TgtOffsets.push_back(TgtBaseOffset);  // 基址偏移
}
```

> 说明：注意 `TgtArgs`（设备指针数组）与 `TgtOffsets`（偏移数组）是**分开**传递的。源码注释解释了原因：某些后端需要 manifest 基指针（如只映射了 `A[N:M]`，内核却要从 `&A[0]` 访问），某些后端则需要 begin 地址本身。分开传让每个插件按需取用。

`PrivateArgumentManagerTy` 把多个小 firstprivate 打包：

[libomptarget/omptarget.cpp:1968-2031](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1968-L2031) —— `packAndTransfer`：把所有待打包的 firstprivate 拼进一个缓冲区，一次 `allocData` + 一次 `submitData` 完成，再把每项的设备指针回填到 `TgtArgs` 对应位置。

```cpp
int packAndTransfer(SmallVector<void *> &TgtArgs) {
  if (!FirstPrivateArgInfo.empty()) {
    char *FirstPrivateArgBuffer = getOrCreateSourceBufferForSubmitData(...);
    std::memset(FirstPrivateArgBuffer, 0, FirstPrivateArgSize);
    // 把每个 firstprivate 按对齐拼进缓冲区
    for (FirstPrivateArgInfoTy &Info : FirstPrivateArgInfo) { ... }
    void *TgtPtr = Device.allocData(FirstPrivateArgSize, FirstPrivateArgBuffer);
    Device.submitData(TgtPtr, FirstPrivateArgBuffer, FirstPrivateArgSize, AsyncInfo);
    // 回填：让 TgtArgs[Info.Index] 指向打包缓冲区里该项的设备地址
    for (FirstPrivateArgInfoTy &Info : FirstPrivateArgInfo)
      TgtArgs[Info.Index] = ...;  // 带对齐 padding 的设备指针
  }
  return OFFLOAD_SUCCESS;
}
```

> 说明：大于阈值（`FirstPrivateArgSizeThreshold = 1024`）或不适合打包的私有参数，会在 `addArg` 时就**立即**分配并传输（见 [omptarget.cpp:1877-1879](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1877-L1879)）。打包只针对"小的、可合并的"firstprivate，目的是减少 H2D 次数。

`processDataAfter` 同样复用 `targetDataEnd`，并把私有内存释放推迟到同步后：

[libomptarget/omptarget.cpp:2238-2276](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2238-L2276) —— `processDataAfter`：调用 `targetDataEnd` 搬回数据，并通过 `addPostProcessingFunction` 把私有参数内存释放挂到同步后执行。

```cpp
static int processDataAfter(...) {
  StateInfoTy StateInfo;
  int Ret = targetDataEnd(Loc, *DeviceOrErr, ArgNum, ...);
  ...
  AsyncInfo.addPostProcessingFunction(
      [PrivateArgumentManager = std::move(PrivateArgumentManager)]() mutable -> int {
        return PrivateArgumentManager.free();  // 同步后才释放私有内存
      });
  return OFFLOAD_SUCCESS;
}
```

> 说明：把释放挂到 `AsyncInfo` 的 post-processing 列表，是因为内核还在跑、私有数据还在用——必须等 `synchronize()` 判定队列空了之后（u2-l7 讲过 `runPostProcessing` 的触发时机）才安全释放。这与 u2-l5 里"`targetDataEnd` 的真正删除推迟到同步后"是同一套机制。

#### 4.4.4 代码实践

**目标**：跟踪 `map(tofrom: a[0:N])` 在 `target` 区域里的完整旅程。

给定代码：

```cpp
int a[100];
#pragma omp target map(tofrom: a[0:10])
{ a[0] = 42; }
```

1. 在 `target()` 的 `processDataBefore` 调用处（[omptarget.cpp:2327](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2327)）下断点/阅读，确认 `ArgTypes[0]` 包含 `OMP_TGT_MAPTYPE_TARGET_PARAM | OMP_TGT_MAPTYPE_TO | OMP_TGT_MAPTYPE_FROM`。
2. 跟进 `targetDataBegin`：确认 `a` 被建映射并 H2D 搬运（因为有 `TO`）。
3. 回到 `processDataBefore` 的参数分流：`a` 命中 `else`（普通 `TARGET_PARAM`）分支，`getTgtPtrBegin` 返回设备指针，压入 `TgtArgs`。
4. 在 `processDataAfter`：跟进 `targetDataEnd`，确认因为 `FROM` 且退出时引用计数归零，触发 D2H 把 `a[0:10]` 搬回。

**需要观察的现象**：内核启动前 `a` 已在设备上，启动后 `a[0]` 的新值（42）被搬回主机。

**预期结果**：你能说清"数据搬运发生在 `launchKernel` 的两侧，且复用了 `target data` 的同一套 begin/end 函数"。

> 若无法运行设备，可改为阅读 [test/offloading/](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/test) 下任意 `target_map.cpp` 类测试的断言来验证行为，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`target` 区域的 `map` 处理与 `target data` 区域的 `map` 处理，在运行时是同一套代码吗？

> **答案**：是。`processDataBefore` / `processDataAfter` 直接调用 u2-l5 的 `targetDataBegin` / `targetDataEnd`。差异仅在于 `target` 额外做了"参数翻译为设备指针"和"私有参数管理"。

**练习 2**：为什么私有参数内存的释放要挂在 `AsyncInfo` 的 post-processing，而不是 `processDataAfter` 里立即释放？

> **答案**：`processDataAfter` 执行时内核可能还在设备上跑、私有数据正被使用。必须等 `synchronize()` 确认本次队列所有操作完成后，post-processing 才会运行，此时释放才安全。

---

### 4.5 落到设备：`launchKernel` 与插件转发

#### 4.5.1 概念说明

走到 `Device.launchKernel(...)` 时，上层已经备齐了一切：设备内核地址 `TgtEntryPtr`、设备指针参数数组 `TgtArgs`、偏移数组 `TgtOffsets`、参数包 `KernelArgs`、异步上下文 `AsyncInfo`。

`DeviceTy` 作为 Facade（u2-l3），自身仍然不碰硬件，它只做一件事：把这些东西原样转发给底层插件的 `RTL->launch_kernel(RTLDeviceID, ...)`。`RTL` 是 `GenericPluginTy*`，`RTLDeviceID` 是插件内的设备下标——这正是 u2-l3 讲过的"双编号映射"对象化的体现。

再往下一层（`GenericPluginTy::launch_kernel` → `GenericDeviceTy::launchKernel`）就进入了 u3-l1/u3-l2 的插件框架范畴。本讲到 `DeviceTy::launchKernel` 为止，把"上层 libomptarget"与"下层插件"的边界划清。

#### 4.5.2 核心流程

```
Device.launchKernel(TgtEntryPtr, TgtArgs, TgtOffsets, KernelArgs, ExtraArgs, AsyncInfo)
        │  DeviceTy（上层 facade）
        ▼
RTL->launch_kernel(RTLDeviceID, TgtEntryPtr, TgtArgs, TgtOffsets,
                   &KernelArgs, KernelExtraArgs, AsyncInfo)
        │  GenericPluginTy（插件框架）
        ▼
GenericDeviceTy::launchKernel(...)   ← 具体 GPU/CPU 后端真正启动内核（u3-l1/u3-l2）
```

#### 4.5.3 源码精读

`DeviceTy::launchKernel` 的声明：

[include/device.h:117-121](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h#L117-L121) —— `launchKernel` 声明：接收内核地址、参数指针数组、偏移数组、参数包、额外参数与异步上下文。

```cpp
// Launch the kernel identified by \p TgtEntryPtr with the given arguments.
int32_t launchKernel(void *TgtEntryPtr, void **TgtVarsPtr,
                     ptrdiff_t *TgtOffsets, KernelArgsTy &KernelArgs,
                     KernelExtraArgsTy *KernelExtraArgs,
                     AsyncInfoTy &AsyncInfo);
```

实现就是一行透明转发：

[libomptarget/device.cpp:358-364](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L358-L364) —— `DeviceTy::launchKernel` 实现：把 `DeviceID` 换成 `RTLDeviceID`，原样转发给插件。

```cpp
int32_t DeviceTy::launchKernel(void *TgtEntryPtr, void **TgtVarsPtr,
                               ptrdiff_t *TgtOffsets, KernelArgsTy &KernelArgs,
                               KernelExtraArgsTy *KernelExtraArgs,
                               AsyncInfoTy &AsyncInfo) {
  return RTL->launch_kernel(RTLDeviceID, TgtEntryPtr, TgtVarsPtr, TgtOffsets,
                            &KernelArgs, KernelExtraArgs, AsyncInfo);
}
```

> 说明：注意入参是上层的 `DeviceID` 语境，但转发时用的是 `RTLDeviceID`（插件内下标）。这正是 u2-l3 强调的：`DeviceTy` 同时持有 `DeviceID`（用户面 UserId）与 `RTLDeviceID`（插件内下标），在边界处完成切换。

插件侧的接球点（本讲的出口，与 u3-l1 衔接）：

[plugins-nextgen/common/src/PluginInterface.cpp:1723-1731](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1723-L1731) —— `GenericPluginTy::launch_kernel`：把 `DeviceId` 解析成具体 `GenericDeviceTy`，再委托其 `launchKernel` 虚函数。

```cpp
int32_t GenericPluginTy::launch_kernel(int32_t DeviceId, void *TgtEntryPtr,
                                       void **TgtArgs, ptrdiff_t *TgtOffsets,
                                       KernelArgsTy *KernelArgs,
                                       KernelExtraArgsTy *KernelExtraArgs,
                                       __tgt_async_info *AsyncInfoPtr) {
  auto Err = getDevice(DeviceId).launchKernel(TgtEntryPtr, TgtArgs, TgtOffsets,
                                              *KernelArgs, KernelExtraArgs,
                                              AsyncInfoPtr);
  ...
}
```

> 说明：`getDevice(DeviceId)` 在插件内把"插件内设备号"映射到具体的 `GenericDeviceTy` 子类实例（如 host 插件的 `HostDeviceTy`、CUDA 插件的 `CUDADeviceTy`）。`GenericDeviceTy::launchKernel` 是虚函数，由各后端 override，最终把内核提交到设备硬件队列（如 CUDA stream）。这之后属于 u3 系列的内容。

#### 4.5.4 代码实践

**目标**：确认"上层到插件"的边界与编号切换。

1. 在 `target()` 的 `Device.launchKernel(...)` 调用处（[omptarget.cpp:2368](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2368)）对照 [device.cpp:358-364](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L358-L364)，确认参数一对一映射。
2. 注意 `launchKernel` 接收的是 `AsyncInfoTy&`，而插件侧 `launch_kernel` 接收的是 `__tgt_async_info*`——这依赖 `AsyncInfoTy` 的 `operator __tgt_async_info*()` 隐式转换（见 [omptarget.h:146](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L146)）。

**需要观察的现象**：`AsyncInfoTy` 在上层是带 post-processing 列表的富对象，到了插件层被"降级"为裸的 `__tgt_async_info`（主要是 Queue 指针）。

**预期结果**：你能指出"编号切换（DeviceID→RTLDeviceID）"和"类型降级（AsyncInfoTy→__tgt_async_info）"都发生在 `DeviceTy::launchKernel` 这一行转发里。

#### 4.5.5 小练习与答案

**练习 1**：`DeviceTy::launchKernel` 为什么几乎只有一行？它存在的意义是什么？

> **答案**：因为 `DeviceTy` 是 Facade，不实现具体逻辑。它的意义是统一接口、并在边界处完成"用户面设备号 → 插件内设备号（RTLDeviceID）"的切换，以及"富 AsyncInfo → 裸 __tgt_async_info"的降级。

**练习 2**：从 `__tgt_target_kernel` 到 `GenericDeviceTy::launchKernel`，`KernelArgs` 经历了哪些变换？

> **答案**：① 入口处按 `Version` 被 `upgradeKernelArgs` 升级补全（含可能的 dyn_ptr 追加）；② `processDataBefore` 后 `NumArgs` 被 `TgtArgs.size()` 覆盖为"设备侧参数个数"；③ 之后以指针 `&KernelArgs` 一路传到插件，结构本身不再改写。

---

## 5. 综合实践

把本讲五个模块串起来，完成一次"端到端调用链标注"。

**任务**：针对下面这个最小程序，画出从 `__tgt_target_kernel` 到 `GenericDeviceTy::launchKernel` 的完整调用链，并在每一处标注 `KernelArgsTy` 各关键字段的来源与流向。

```cpp
#include <cstdio>
int main() {
  int x = 0;
  #pragma omp target map(tofrom: x)
  { x = 41; }
  printf("%d\n", x);  // 预期 41
}
```

**要求**：

1. 用一张流程图（文字版即可）标出 6 个关键站点：
   - `__tgt_target_kernel`（[interface.cpp:462](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L462)）
   - `targetKernel`（[interface.cpp:370](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L370)）
   - `target`（[omptarget.cpp:2286](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2286)）
   - `processDataBefore`（[omptarget.cpp:2055](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2055)）
   - `Device.launchKernel`（[omptarget.cpp:2368](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2368)）
   - `RTL->launch_kernel`（[device.cpp:362](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L362)）
2. 针对本程序的 `x`，逐字段填表：

   | `KernelArgsTy` 字段 | 本例中的值/来源 |
   | --- | --- |
   | `Version` | 由编译器按其版本填，`targetKernel` 据此升级 |
   | `NumArgs` | 初始为编译器给的值；`processDataBefore` 后被 `TgtArgs.size()` 覆盖 |
   | `ArgPtrs[0]` | `&x`（主机地址） |
   | `ArgTypes[0]` | `TARGET_PARAM | TO | FROM`（标量 `tofrom`） |
   | `ArgSizes[0]` | `sizeof(int) = 4` |
   | `Tripcount` | 非 teams 循环，为 0 |
   | `UserNumBlocks[0]` | 非 teams 区域，`targetKernel` 里置为 1 |

3. 在图中标出**数据搬运发生的两个时机**（`processDataBefore` 内 H2D、`processDataAfter` 内 D2H），并说明它们复用了 u2-l5 的哪两个函数。

**预期结果**：你能脱稿讲清"`x` 如何从主机到达内核、内核如何被找到并启动、结果又如何回到主机"这整个闭环。

## 6. 本讲小结

- **入口极薄**：`__tgt_target_kernel` 唯一逻辑是按 `nowait` 选异步上下文类型，其余交给 `targetKernel` 模板。
- **版本协商在前**：`targetKernel` 先用 `upgradeKernelArgs` 按 `Version` 把 `KernelArgsTy` 升级补全（含 dyn_ptr 隐式参数），主流程拿到的永远是统一格式；`KernelArgsTy` 靠首字段 `Version` + `static_assert` 保证 ABI 兼容。
- **HostPtr 是内核定位键**：`target()` 通过 `getTableMap` 把主机桩地址映射到 `(TranslationTable, Index)`，再用 `DeviceId` 取出该设备已加载入口视图，最后 `EntriesBegin[Index].Address` 得到设备内核地址。
- **启动与映射紧耦合**：`target()` 直接复用 u2-l5 的 `targetDataBegin` / `targetDataEnd`（包在 `processDataBefore` / `processDataAfter` 里），数据搬运发生在 `launchKernel` 两侧；私有参数由 `PrivateArgumentManagerTy` 打包传输、同步后才释放。
- **Facade 在边界切换**：`DeviceTy::launchKernel` 只有一行转发，但在这一行完成了"用户面 `DeviceID` → 插件内 `RTLDeviceID`"与"`AsyncInfoTy` → 裸 `__tgt_async_info`"两件事，把上层与插件层解耦。
- **同步与结果判定在 `targetKernel`**：`target()` 只负责把操作提交进队列，真正"等内核跑完"由 `targetKernel` 的 `AsyncInfo.synchronize()` 完成，再由 `handleTargetOutcome` 处理成败。

## 7. 下一步学习建议

- **u2-l7 异步执行与 AsyncInfoTy**：本讲多次出现 `AsyncInfo.synchronize()` 与 `addPostProcessingFunction`，下一讲会讲清 `BLOCKING` / `NON_BLOCKING` 同步、post-processing 触发时机，以及 `nowait` 区域如何挂在 OpenMP task 上。
- **u3-l1 / u3-l2 通用插件接口**：本讲到 `GenericPluginTy::launch_kernel` 为止。若想看内核在设备上真正如何启动，应继续阅读 `GenericDeviceTy::launchKernel` 虚函数与 `GenericKernelTy`。
- **u3-l8 内核录制与重放**：本讲提到的 `KernelExtraArgsTy::ReplayOutcome` 与 `target_replay`（[omptarget.cpp:2409](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2409)）是录制重放机制的入口，后续会专门讲解。
- **复习 u2-l5**：若对 `processDataBefore` / `processDataAfter` 内部的引用计数与搬运条件仍有疑问，建议回看 u2-l5 的 `targetDataBegin` / `targetDataEnd` 三阶段流程。
