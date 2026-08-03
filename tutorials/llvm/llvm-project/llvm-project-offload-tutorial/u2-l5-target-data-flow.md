# target data begin/end/update 流程

## 1. 本讲目标

在 [u2-l4](u2-l4-data-mapping.md) 里，我们打开了 `DeviceTy` 内嵌的 `MappingInfoTy`，看清了「映射表 + 双引用计数 + `getTargetPointer`/`getTgtPtrBegin` 两条算法」这一台机器的内部构造。但真实的 OpenMP 程序并不会直接调用这两个函数——编译器为每一条 `target data` / `target enter data` / `target exit data` / `target update` 指令生成的是 `__tgt_target_data_begin_mapper` / `end_mapper` / `update_mapper` 这一族入口。本讲要回答的是：**从这些入口到底层映射算法之间，运行时是如何「指挥」的。**

学完后你应当掌握：

1. `target data` 的三个阶段——**begin（进入）/ end（退出）/ update（更新）**——在职责上的根本差异：谁负责建立映射、谁负责搬运、谁改变引用计数、谁负责释放。
2. 一条 `map(to:)` / `map(from:)` 子句在运行时如何被逐条解析（跳过哪些、修改哪些标志、调用映射层的哪个函数），最终变成一次 `submitData`（主机→设备）或 `retrieveData`（设备→主机）。
3. 引用计数的「+1 / -1」与数据传输的「触发时机」分别落在 begin 与 end 的哪一段代码里，以及为什么真正的设备内存删除要推迟到同步之后。

本讲聚焦的最小模块是 `libomptarget`，主要落在 [libomptarget/omptarget.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp) 与 [libomptarget/interface.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp)。它直接建立在 [u2-l4](u2-l4-data-mapping.md) 的映射算法之上，并为下一讲 [u2-l6](u2-l6-kernel-launch-flow.md) 的内核启动流程做铺垫——因为内核启动前后正是复用了 begin/end 这套流程。

## 2. 前置知识

进入源码前，先建立三个直觉。

**直觉一：三个阶段对应三种 OpenMP 指令。** Clang 把不同的 `target` 指令编译成三组入口：

| OpenMP 指令 | 运行时入口 | 对应内部函数 | 语义 |
|-------------|-----------|-------------|------|
| `target enter data map(to:)`、`target data` 的**进入**、`target` 区域的**进入** | `__tgt_target_data_begin_mapper` | `targetDataBegin` | 建立映射、可选地主机→设备搬运 |
| `target exit data map(from:)`、`target data` 的**退出**、`target` 区域的**退出** | `__tgt_target_data_end_mapper` | `targetDataEnd` | 可选地设备→主机搬运、递减引用计数、可能删除 |
| `target update to/from(:)` | `__tgt_target_data_update_mapper` | `targetDataUpdate` | 单向搬运，**不改引用计数、不分配、不释放** |

记住这张表，本讲后面所有代码都是为了实现它。

**直觉二：「搬运」和「计数」是两件正交的事。** 这是 [u2-l4](u2-l4-data-mapping.md) 已建立、本讲反复用到的核心结论。引用计数决定「设备内存何时分配、何时释放」；map 子句的 `to`/`from`/`always` 标志决定「数据何时在主机与设备之间搬运」。begin 负责 **+1 计数 + 可能搬运**，end 负责 **-1 计数 + 可能搬运 + 可能删除**，update 负责 **只搬运、不碰计数**。本讲的任务就是把这三句话在源码里逐行落实。

**直觉三：删除是「延迟」的。** end 阶段即便发现引用计数归零（`IsLast`），也**不会立即**释放设备内存——它把删除任务挂到 `AsyncInfo` 的「后处理（post-processing）」队列里，等设备队列同步完成、所有搬运都落地之后再删。这是因为设备搬运是异步的，提前删除会与尚未完成的 `retrieveData` 竞争。

如果你对 `tgt_map_type` 位标志（`OMP_TGT_MAPTYPE_TO`/`FROM`/`MEMBER_OF`/`ATTACH` 等）还不熟，请先复习 [u1-l5](u1-l5-compiler-runtime-contract.md)；本讲把它们当作已知输入。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [libomptarget/interface.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp) | `__tgt_target_data_begin_mapper` 等入口实现，以及把它们统一收敛进 `targetData<>` 模板的「公共骨架」。 |
| [libomptarget/omptarget.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp) | `targetDataBegin` / `targetDataEnd` / `targetDataUpdate` / `targetDataMapper` / `processAttachEntries` / `postProcessingTargetDataEnd` / `targetDataContiguous` 的实现，是本讲的主战场。 |
| [include/OpenMP/Mapping.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h) | `StateInfoTy`（跨 begin/end 追踪分配与 FROM 跳过信息）、`TargetDataFuncPtrTy`、`targetDataBegin/End/Update` 的声明。 |
| [libomptarget/OpenMP/Mapping.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp) | `getTargetPointer`（begin 调用，+1 计数 + H2D）与 `getTgtPtrBegin`（end/update 调用，-1 计数）的实现（[u2-l4](u2-l4-data-mapping.md) 已精读，本讲引用其触发条件）。 |
| [include/omptarget.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h) | `tgt_map_type` 枚举（第 49–91 行）与 `__tgt_target_data_*_mapper` 入口签名（第 348–407 行）。 |
| [libomptarget/private.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/private.h) | `printKernelArguments`——把每条 map 的 `ArgType` 翻译成 `to`/`from`/`tofrom` 等可读字符串，是本讲实践的观察利器。 |

**分层关系**：本讲是「指挥层」。它解析 map 数组、决定调用顺序与触发条件，把真正干活的事（查表、分配、搬运、删除）委托给 [u2-l4](u2-l4-data-mapping.md) 的 `MappingInfoTy` 与 [u2-l3](u2-l3-device-abstraction.md) 的 `DeviceTy`（`submitData`/`retrieveData`/`allocData`/`deleteData`）。

## 4. 核心概念与源码讲解

### 4.1 统一驱动层：`interface.cpp` 的 `targetData` 模板

#### 4.1.1 概念说明

虽然 begin / end / update 语义不同，但它们的「外壳」几乎一样：检查设备、获取 `DeviceTy`、构造异步上下文 `AsyncInfoTy`、调用对应的内部函数、处理 ATTACH、同步、上报结果。为了避免三份重复代码，运行时用一个 C++ 函数模板 `targetData<>` 把这套外壳抽出来，三个入口只是往里塞「不同的内部函数指针」。

#### 4.1.2 核心流程

`targetData<>` 模板的执行步骤（伪代码）：

```
targetData(Loc, DeviceId, ArgNum, ..., TargetDataFunction, RegionName):
  1. checkDevice(DeviceId)           # 设备是否就绪/是否回退到主机
  2. 打印 kernel arguments（若开了 OMP_INFOTYPE_KERNEL_ARGS）
  3. Device = PM->getDevice(DeviceId) # 取得 DeviceTy
  4. AsyncInfo = TargetAsyncInfoTy(Device)   # 构造异步上下文
  5. 若是 begin/end：分配 StateInfoTy       # update 不需要
  6. Rc = TargetDataFunction(..., AsyncInfo, StateInfo, FromMapper=false)
  7. 若 Rc==OK 且有 ATTACH 条目：processAttachEntries(...)   # 仅 begin 会有
  8. 若 Rc==OK：AsyncInfo.synchronize()      # 同步设备队列 + 跑后处理
  9. handleTargetOutcome(Rc==OK)             # 失败时按策略报错
```

关键点有两个：

- **`TargetDataFunction` 是一个函数指针**，类型 `TargetDataFuncPtrTy`，可以是 `targetDataBegin` / `targetDataEnd` / `targetDataUpdate` 之一。这就是「同一骨架驱动三阶段」的机制。
- **`StateInfoTy` 只在 begin/end 时分配**（update 传 `nullptr`）。它是一个「跨递归（处理 mapper 时）追踪本次构造内部分配、ATTACH、跳过的 FROM」的状态包，详见 4.1.3。

#### 4.1.3 源码精读

模板本体在 [interface.cpp:114-194](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L114-L194)，其中分配 `StateInfo` 与「调用→处理 ATTACH→同步」三步：

```cpp
// interface.cpp:175-191
std::unique_ptr<StateInfoTy> StateInfo;
if (TargetDataFunction == targetDataBegin ||
    TargetDataFunction == targetDataEnd)
  StateInfo = std::make_unique<StateInfoTy>();

Rc = TargetDataFunction(Loc, *DeviceOrErr, ArgNum, ArgsBase, Args, ArgSizes,
                        ArgTypes, ArgNames, ArgMappers, AsyncInfo,
                        StateInfo.get(), /*FromMapper=*/false);

if (Rc == OFFLOAD_SUCCESS) {
  // Process deferred ATTACH entries BEFORE synchronization
  if (StateInfo && !StateInfo->AttachEntries.empty())
    Rc = processAttachEntries(*DeviceOrErr, *StateInfo, AsyncInfo);

  if (Rc == OFFLOAD_SUCCESS)
    Rc = AsyncInfo.synchronize();
}
```

注意第 186 行的注释 **"Process deferred ATTACH entries BEFORE synchronization"**——ATTACH 处理必须在同步之前，因为 ATTACH 本身会向设备队列里提交 `submitData`（更新设备指针），而 `synchronize` 之后队列就空了。`AsyncInfo.synchronize()` 不仅等待设备，还会运行挂在 `AsyncInfo` 上的后处理函数（end 阶段把删除任务挂在这里，见 4.3.4）。

三个入口都只是对模板的薄封装，差别只在传入的内部函数与字符串。以 begin 为例：

```cpp
// interface.cpp:199-210
EXTERN void __tgt_target_data_begin_mapper(ident_t *Loc, int64_t DeviceId,
                                           int32_t ArgNum, void **ArgsBase,
                                           void **Args, int64_t *ArgSizes,
                                           int64_t *ArgTypes,
                                           map_var_info_t *ArgNames,
                                           void **ArgMappers) {
  ...
  targetData<AsyncInfoTy>(Loc, DeviceId, ArgNum, ArgsBase, Args, ArgSizes,
                          ArgTypes, ArgNames, ArgMappers, targetDataBegin,
                          "Entering OpenMP data region with being_mapper",
                          "begin");
}
```

end 与 update 的封装见 [interface.cpp:227-237](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L227-L237) 与 [interface.cpp:251-263](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L251-L263)。`nowait` 变体（如 [`__tgt_target_data_begin_nowait_mapper`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L212-L222)）只是把模板参数换成 `TaskAsyncInfoWrapperTy`，把异步上下文挂到 OpenMP task 上（详见 [u2-l7](u2-l7-async-model.md)）。

`StateInfoTy` 本身是一个「本次构造的临时账本」，定义在 [Mapping.h:501-602](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L501-L602)，含四张表：

| 字段 | 用途 | 填写者 |
|------|------|--------|
| `AttachEntries` | 延迟处理的 ATTACH 条目 | begin |
| `NewAllocations` | 本次新分配的主机指针→大小 | begin |
| `SkippedFromEntries` | 因引用计数未归零而**跳过**的 FROM | end |
| `ReleasedEntries` / `TransferredFromEntries` | 已释放 / 已搬运的 FROM | end |

这张账本是 begin 与 end 处理「同一段内存被多个 map 条目引用」这类复杂情形的关键（见 4.3.2）。

#### 4.1.4 代码实践

**实践目标**：验证「三个入口共用同一套外壳」。

**操作步骤**：

1. 打开 [interface.cpp:199-276](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L199-L276)。
2. 逐个对比 `__tgt_target_data_begin_mapper` / `end_mapper` / `update_mapper` 三个函数体。
3. 找出它们**唯一**的差别（提示：传入的函数指针与字符串字面量）。

**预期结果**：三者体形完全一致，都是一行 `targetData<...>(...)`，仅第 10、11 个实参不同。这从源码层面证明了「三阶段同一骨架」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `StateInfo` 在 update 路径下是 `nullptr`？

> **答案**：update 既不分配/删除设备内存，也不改引用计数，因此没有 ATTACH、没有新分配、没有需要延迟的 FROM，`StateInfo` 这本账本无事可记，传 `nullptr` 即可；内部函数 `targetDataUpdate` 也不会解引用它。

**练习 2**：`nowait` 变体与同步变体在调用 `targetData` 时有什么区别？

> **答案**：仅模板参数不同——同步变体用 `AsyncInfoTy`，`nowait` 变体用 `TaskAsyncInfoWrapperTy`，使异步上下文挂到 OpenMP task 上，便于后续 `__tgt_target_nowait_query` 查询完成（见 [u2-l7](u2-l7-async-model.md)）。

---

### 4.2 `targetDataBegin`：进入区域，建立映射 + 主机→设备搬运

#### 4.2.1 概念说明

`targetDataBegin` 负责「进入」一侧：遍历本次构造的所有 map 条目，为每一段主机内存**建立或复用**映射（必要时在设备上分配），并在满足条件时把数据从主机搬到设备（H2D）。它本身**不做 H2D 搬运的细节判断**——而是把这些委托给 [u2-l4](u2-l4-data-mapping.md) 的 `getTargetPointer`；begin 的职责是：解析每条 map、跳过不该处理的、准备好调用 `getTargetPointer` 所需的实参、并把 ATTACH 条目收集起来延迟处理。

#### 4.2.2 核心流程

对 `ArgNum` 条 map 自前向后（`I = 0 .. ArgNum-1`）逐条处理：

```
targetDataBegin(...):
  for I in 0..ArgNum-1:
    if LITERAL 或 PRIVATE:  continue        # 无映射需求，跳过
    if 有自定义 mapper:     targetDataMapper(...) 后 continue   # 见 4.5
    if ATTACH:              记入 StateInfo->AttachEntries 后 continue  # 延迟
    计算 TgtPadding（组合结构体对齐）
    UpdateRef = !(MEMBER_OF)                # 顶层条目才更新引用计数
    TPR = getTargetPointer(..., HasFlagTo, HasFlagAlways, UpdateRef, ...)
          # ↑ 内部：新建则分配+refcount=1；已存在则 incRefCount(+1)；
          #         若 HasFlagTo 且(新条目|always|本构造新分配) → submitData(H2D)
    记录 NewAllocations（若 IsNewEntry）
    若 PTR_AND_OBJ：performPointerAttachment(...)   # 更新设备指针
```

引用计数 +1 与 H2D 搬运的**触发条件**（落在 `getTargetPointer` 内，[u2-l4](u2-l4-data-mapping.md) 已精读）：

- **+1 计数**：仅当 `UpdateRef == true`，即该条目不是结构体成员（`!(MEMBER_OF)`）。结构体成员的计数随父条目走。
- **H2D 搬运**：当且仅当 `HasFlagTo && (IsNewEntry || HasFlagAlways || WasNewlyAllocatedForCurrentRegion) && Size != 0`。也就是「有 `to`」且「是新映射、或带 `always`、或本构造内刚分配过」时才搬。

用真值表总结：

| `to` 标志 | 新条目 | `always` | 是否 H2D |
|-----------|--------|----------|----------|
| 无 | — | — | 否 |
| 有 | 是 | — | 是 |
| 有 | 否 | 否 | 否（数据已在设备，避免重复搬） |
| 有 | 否 | 是 | 是（`always` 强制） |

#### 4.2.3 源码精读

主循环开头跳过三类条目：

```cpp
// omptarget.cpp:524-528
for (int32_t I = 0; I < ArgNum; ++I) {
  // Ignore private variables and arrays - there is no mapping for them.
  if ((ArgTypes[I] & OMP_TGT_MAPTYPE_LITERAL) ||
      (ArgTypes[I] & OMP_TGT_MAPTYPE_PRIVATE))
    continue;
```

`LITERAL`（按值传递的 firstprivate）与 `PRIVATE`（私有变量）都没有设备映射，直接跳过。随后是自定义 mapper 的分发（见 4.5）。

ATTACH 条目被**延迟**处理——先收进 `StateInfo->AttachEntries`，待所有映射建完再统一处理：

```cpp
// omptarget.cpp:560-576
if (ArgTypes[I] & OMP_TGT_MAPTYPE_ATTACH) {
  const bool IsCorrespondingPointerInit =
      (ArgTypes[I] & OMP_TGT_MAPTYPE_PRIVATE);
  if (!IsCorrespondingPointerInit)
    StateInfo->AttachEntries.emplace_back(/*PointerBase=*/HstPtrBase, ...);
  ODBG(ODT_Mapping) << "Deferring ATTACH map-type processing ...";
  continue;
}
```

接着计算组合结构体所需的对齐填充 `TgtPadding`（[omptarget.cpp:581-591](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L581-L591)），再决定 `UpdateRef`：

```cpp
// omptarget.cpp:608
bool UpdateRef = !(ArgTypes[I] & OMP_TGT_MAPTYPE_MEMBER_OF);
```

这段代码上方有一段重要注释（第 603–607 行）解释：之所以用 `MEMBER_OF` 而非 `TARGET_PARAM` 来判断，是因为 `target data map`（不带 target 区域）不会给任何条目标 `TARGET_PARAM`，用 `MEMBER_OF` 才能正确区分「顶层条目」与「结构体成员」。作者自嘲 "This may be considered a hack"。

核心调用是把所有判断结果打包传给 `getTargetPointer`：

```cpp
// omptarget.cpp:664-668
auto TPR = Device.getMappingInfo().getTargetPointer(
    HDTTMap, HstPtrBegin, HstPtrBase, TgtPadding, DataSize, HstPtrName,
    HasFlagTo, HasFlagAlways, IsImplicit, UpdateRef, HasCloseModifier,
    HasPresentModifier, HasHoldModifier, AsyncInfo, PointerTpr.getEntry(),
    /*ReleaseHDTTMap=*/true, StateInfo);
```

注意最后一个实参 `StateInfo`——它让 `getTargetPointer` 内部能查询「本构造是否刚分配过同一指针」，从而支持 `map(alloc:)` 后再 `map(to:)` 仍触发搬运的情形（见 [Mapping.cpp:345-360](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L345-L360) 中的 `WasNewlyAllocatedForCurrentRegion`）。**H2D 的真正触发条件就落在 `getTargetPointer` 内部**：

```cpp
// Mapping.cpp:358-361
if (LR.TPR.TargetPointer && !LR.TPR.Flags.IsHostPointer && HasFlagTo &&
    (LR.TPR.Flags.IsNewEntry || HasFlagAlways ||
     WasNewlyAllocatedForCurrentRegion()) &&
    Size != 0) {
```

满足条件即调用 [`Device.submitData`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L387-L388) 完成 H2D 搬运（[u2-l3](u2-l3-device-abstraction.md) 讲过 `submitData` 最终转发到插件）。

回到 begin 主循环，最后记录新分配信息，供后续 ATTACH 决策使用：

```cpp
// omptarget.cpp:696-698
if (TPR.Flags.IsNewEntry && !IsHostPtr && TgtPtrBegin)
  StateInfo->NewAllocations[HstPtrBegin] = DataSize;
```

#### 4.2.4 代码实践

**实践目标**：观察 `map(to:)` 真正触发一次 H2D 搬运时打印了什么。

**操作步骤**：

1. 编译一个最小程序（host 插件即可）：

   ```c
   // tdb.c —— 示例代码
   #include <stdio.h>
   int main(void) {
     int x = 42;
     #pragma omp target data map(to: x)
     {
       #pragma omp target map(tofrom: x)
       x += 1;
     }
     printf("x=%d\n", x);
     return 0;
   }
   ```

   ```bash
   clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu tdb.c -o tdb
   LIBOMPTARGET_INFO=63 ./tdb
   ```

2. 在输出里寻找两类行：
   - `Entry ...: Type=...`：由 [`printKernelArguments`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/private.h#L57-L86) 打印，`Type` 字段直接来自 `printKernelArguments` 把 `ArgType` 位翻译成的 `to`/`from`/`tofrom`/`alloc` 等字符串。
   - `Creating new map entry ...` 或 `Mapping exists ...`：由 `getTargetPointer` 打印（[Mapping.cpp:307-316](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L307-L316) 与 245–251 行）。
3. 对照 [printKernelArguments 的 Type 翻译逻辑](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/private.h#L63-L82)，核对每条 map 被识别成了什么。

**需要观察的现象**：`x` 第一次进入 `map(to:)` 时应出现 `Creating new map entry`（新条目，refcount=1）且伴随一次主机→设备搬运；进入内层 `target map(tofrom:)` 时由于已存在，应出现 `Mapping exists` 且 refcount 递增到 2、**不再**搬运（因为非 `always` 且非新条目）。

**预期结果**：能从日志中区分出「新条目 + 搬运」与「复用条目 + 仅 +1 计数」两种情形。**待本地验证**（具体日志行格式可能随版本微调）。

#### 4.2.5 小练习与答案

**练习 1**：`map(tofrom: x)` 在 begin 阶段会搬运吗？

> **答案**：会。`tofrom` 同时置 `OMP_TGT_MAPTYPE_TO` 与 `OMP_TGT_MAPTYPE_FROM`，begin 阶段只看 `TO` 位。若是新条目则触发一次 H2D 搬运；`FROM` 位在 begin 阶段被忽略，留给 end 阶段。

**练习 2**：为什么结构体成员条目（`MEMBER_OF`）在 begin 时 `UpdateRef=false`？

> **答案**：成员的设备内存随父条目一起分配，其生命周期由父条目的引用计数统一管理；若成员也 +1，会导致父条目释放后成员计数仍非零，破坏「整体分配、整体释放」的语义。

---

### 4.3 `targetDataEnd`：退出区域，设备→主机搬运 + 引用计数递减 + 延迟删除

#### 4.3.1 概念说明

`targetDataEnd` 负责「退出」一侧，是三个函数里最复杂的：它要（可能）把数据从设备搬回主机（D2H）、递减引用计数、在引用计数归零时安排删除。和 begin 对称地，它把「-1 计数」与「IsLast 预测」委托给 `getTgtPtrBegin`，自己负责解析条目、决定 FROM 是否搬运、处理跳过/已释放的 FROM、并把删除任务挂到后处理队列。

#### 4.3.2 核心流程

```
targetDataEnd(...):
  PostProcessingPtrs = new SmallVector<PostProcessingInfo>
  for I in (ArgNum-1) .. 0:                 # ★逆序遍历★
    if LITERAL 或 PRIVATE:        continue
    if ATTACH:                    continue   # ATTACH 只在进入侧生效
    if 有自定义 mapper:           targetDataMapper(...) 后 continue
    TPR = getTgtPtrBegin(..., UpdateRef, HasHold, MustContain=!IsImplicit,
                          ForceDelete, FromDataEnd=true)
          # ↑ 内部：decShouldRemove 预测是否归零(IsLast)；decRefCount(-1)
    if 不存在:                    continue   # 退出时不存在则忽略
    if IsLast: 记入 StateInfo->ReleasedEntries
    # —— FROM 搬运决策 ——
    if (HasFrom && 非主机指针 && Size!=0) && (IsLast || always || 曾被释放):
        PerformFromRetrieval(...)            # retrieveData(D2H)
    elif (HasFrom && 非主机指针 && Size!=0):
        记入 StateInfo->SkippedFromEntries   # 暂不搬，等归零再说
    if IsLast 且有跳过的 FROM 落在本释放区间内: 补搬它们
    把 (HstPtr,Size,ArgType,TPR) 放进 PostProcessingPtrs，并先解锁 entry
  AsyncInfo.addPostProcessingFunction(postProcessingTargetDataEnd(...))
```

FROM（设备→主机）搬运的触发条件是本节核心，整理成真值表：

| `from` 标志 | `IsLast`（计数归零） | `always` | 曾被释放 | 是否 D2H |
|-------------|---------------------|----------|----------|----------|
| 无 | — | — | — | 否 |
| 有 | 是 | — | — | 是 |
| 有 | 否 | 是 | — | 是 |
| 有 | 否 | 否 | 是 | 是（补救） |
| 有 | 否 | 否 | 否 | 否（记为 skipped） |

#### 4.3.3 源码精读

**逆序遍历**是 end 的第一个特征：

```cpp
// omptarget.cpp:1088
for (int32_t I = ArgNum - 1; I >= 0; --I) {
```

逆序是为了与 begin 的正序对应——`target data` 的语义是「栈式」的，先进入的最后退出。紧接着跳过 `ATTACH`（[omptarget.cpp:1098-1101](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1098-L1101)），因为 ATTACH 只在进入侧有意义。

核心调用是 `getTgtPtrBegin`，注意 `UpdateRef` 的判定与 begin 略有不同：

```cpp
// omptarget.cpp:1128-1137
bool UpdateRef = !(ArgTypes[I] & OMP_TGT_MAPTYPE_MEMBER_OF) ||
                 (ArgTypes[I] & OMP_TGT_MAPTYPE_PTR_AND_OBJ);
bool ForceDelete = ArgTypes[I] & OMP_TGT_MAPTYPE_DELETE;
...
TargetPointerResultTy TPR = Device.getMappingInfo().getTgtPtrBegin(
    HstPtrBegin, DataSize, UpdateRef, HasHoldModifier, !IsImplicit,
    ForceDelete, /*FromDataEnd=*/true);
```

end 比 begin 多认一种「该 -1」的情形：`PTR_AND_OBJ` 即便是成员也要 -1（因为进入时它作为独立对象分配过）。`getTgtPtrBegin` 内部用 `decShouldRemove` **预测**递减后是否归零，得到 `IsLast`，再真正 `decRefCount`（[Mapping.cpp:431-463](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L431-L463)）。注意删除并非在此处发生——`getTgtPtrBegin` 只递减计数并标记 `IsLast`，**不删除**。

FROM 搬运的决策是 end 最精巧的部分（[omptarget.cpp:1290-1330](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1290-L1330)）：

```cpp
// omptarget.cpp:1290-1298
bool IsMapFromOnNonHostNonZeroData =
    HasFrom && !TPR.Flags.IsHostPointer && DataSize != 0;

auto IsLastOrHasAlwaysOrWasReleased = [&]() {
  return TPR.Flags.IsLast || HasAlways || WasPreviouslyReleased();
};

if (IsMapFromOnNonHostNonZeroData && IsLastOrHasAlwaysOrWasReleased()) {
  Ret = PerformFromRetrieval(HstPtrBegin, TgtPtrBegin, DataSize, TPR.getEntry());
```

真正搬运由内部 lambda `PerformFromRetrieval` 完成，它调用 [`Device.retrieveData`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1239)（D2H），并在 `IsLast` 时为该条目加一个 Event，防止删除与并发拷贝竞争（[omptarget.cpp:1250-1253](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1250-L1253)）。

若 `HasFrom` 但条件不满足（计数未归零、非 always），则**暂不搬**，记入 `SkippedFromEntries`：

```cpp
// omptarget.cpp:1325
StateInfo->addSkippedFromEntry(HstPtrBegin, DataSize);
```

随后，当某个条目真的 `IsLast` 归零时，会回头检查这些被跳过的 FROM 是否落在它的区间内，若是则**补搬**（[omptarget.cpp:1334-1373](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1334-L1373)）。这处理了「`p1 = p2 = &x; map(delete:p1) map(from:p2)`」这类同一存储被多个列表项引用、且 FROM 先于归零被遇到的合法情形。

#### 4.3.4 后处理：真正删除发生在同步之后

循环结束后，end **不立即删除**任何设备内存，而是把所有待处理条目打包挂到 `AsyncInfo` 的后处理队列：

```cpp
// omptarget.cpp:1376-1386
PostProcessingPtrs->emplace_back(HstPtrBegin, DataSize, ArgTypes[I],
                                 std::move(TPR));
PostProcessingPtrs->back().TPR.getEntry()->unlock();   // 先解锁，允许复用
...
AsyncInfo.addPostProcessingFunction([=, Device = &Device]() mutable -> int {
  return postProcessingTargetDataEnd(Device, *PostProcessingPtrs);
});
```

这个后处理函数会在 [u2-l7](u2-l7-async-model.md) 讲的 `AsyncInfoTy::synchronize()` 完成设备同步、搬运落地后被调用。`postProcessingTargetDataEnd`（[omptarget.cpp:990-1075](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L990-L1075)）做两件事：

1. **若有 `from`，还原影子指针**：把主机指针/描述符恢复成进入区域前的原值（[omptarget.cpp:1035-1054](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1035-L1054)），这是 [u2-l4](u2-l4-data-mapping.md) 影子指针机制的退出侧配合。
2. **若 `DelEntry` 仍成立，真正删除**：

```cpp
// omptarget.cpp:1063-1066
Ret = Device->getMappingInfo().eraseMapEntry(HDTTMap, Entry, DataSize);
HDTTMap.destroy();
Ret |= Device->getMappingInfo().deallocTgtPtrAndEntry(Entry, DataSize);
```

`eraseMapEntry` 从映射表摘除条目，`deallocTgtPtrAndEntry` 释放设备内存（转发到插件的 `deleteData`）。这里还有一道并发保护：`decDataEndThreadCount()` 确认本线程是否真是最后一个使用该条目的线程，避免多线程场景下重复删除（[omptarget.cpp:1021-1027](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1021-L1027)）。

#### 4.3.5 代码实践

**实践目标**：追踪一次 `map(from:)` 的 D2H 搬运与延迟删除。

**操作步骤**：

1. 修改 4.2.4 的程序，把内层换成会修改设备值并要求回传的结构：

   ```c
   // tde.c —— 示例代码
   #include <stdio.h>
   int main(void) {
     int a[4] = {0,0,0,0};
     #pragma omp target data map(from: a)   // 退出时回传
     {
       #pragma omp target map(tofrom: a)
       for (int i = 0; i < 4; ++i) a[i] = i * 10;
     }
     printf("%d %d %d %d\n", a[0], a[1], a[2], a[3]);
     return 0;
   }
   ```

2. 用 `LIBOMPTARGET_INFO=63 ./tde` 运行。
3. 在输出里定位退出阶段的 `Mapping exists ... (decremented)` 与 `(decremented, delayed deletion)` 字样，以及 `Moving N bytes (tgt:...) -> (hst:...)`（来自 [omptarget.cpp:1227-1228](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1227-L1228)）。

**需要观察的现象**：`target data map(from:)` 的退出对应一次 D2H；而内层 `target map(tofrom:)` 退出时由于外层 `target data` 仍持有引用（计数未归零），应只 `decremented`、不 `delayed deletion`、也不立即 D2H——这正好印证「嵌套区域内层退出不释放、不回传」。

**预期结果**：能区分「递减但保留」与「归零并安排延迟删除」。**待本地验证**。

#### 4.3.6 小练习与答案

**练习 1**：为什么 `targetDataEnd` 要逆序遍历，而 `targetDataBegin` 正序？

> **答案**：map 子句形成的映射是栈式的，后进入的条目可能依赖先进入的（如 ATTACH 依赖 pointee 已映射）。逆序退出保证依赖被先撤销，与进入顺序对称，避免在 pointee 仍被引用时提前释放。

**练习 2**：把设备内存删除推迟到 `synchronize` 之后，解决了什么问题？

> **答案**：D2H 搬运（`retrieveData`）是异步提交到设备队列的。若计数归零就立刻 `deleteData`，可能与尚未完成的搬运竞争同一块设备内存。推迟到同步完成后删除，保证所有在途搬运都已落地。

**练习 3**：`map(delete: x)` 与普通 `map(from: x)` 退出时行为有何不同？

> **答案**：`delete` 置 `OMP_TGT_MAPTYPE_DELETE`，使 `ForceDelete=true`；`getTgtPtrBegin` 会 `resetRefCount` 强制归零并标记 `IsLast`，从而无论原计数多少都安排删除（[Mapping.cpp:434-439](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L434-L439)）。若同时有 `from`，仍会先搬运再删除。

---

### 4.4 `targetDataUpdate`：不改变引用计数的纯搬运

#### 4.4.1 概念说明

`target update to/from(:)` 是 OpenMP 里唯一「只搬数据、不动映射」的指令：它既不分配设备内存、不删除设备内存，也不改引用计数。运行时只需查到既有映射对应的设备指针，按 `to`/`from` 方向做一次搬运即可。对应实现是 `targetDataUpdate`，真正干活的是它调用的 `targetDataContiguous`（与非连续的 `targetDataNonContiguous`）。

#### 4.4.2 核心流程

```
targetDataUpdate(...):
  for I in 0..ArgNum-1:
    if LITERAL 或 PRIVATE:  continue
    if 有自定义 mapper:     targetDataMapper(...) 后 continue
    if NON_CONTIG:  targetDataNonContiguous(...)   # 多维非连续
    else:           targetDataContiguous(...)
```

`targetDataContiguous` 的核心是**以 `UpdateRefCount=false` 查映射**，然后按方向搬运：

```
TPR = getTgtPtrBegin(HstPtr, Size, UpdateRefCount=false, MustContain=true)
if 不存在:   若 present 则报错，否则 no-op
if 主机指针: no-op（统一内存）
if TO:   Device.submitData(...)    # H2D
if FROM: Device.retrieveData(...)  # D2H
```

注意 `MustContain=true`：update 要求查询必须完全落在某个既有映射区间内，不允许「延伸」命中——因为 update 不分配，延伸命中没有设备内存可用。

#### 4.4.3 源码精读

`targetDataContiguous` 查映射时显式传 `UpdateRefCount=false`（[omptarget.cpp:1394-1396](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1394-L1396)）：

```cpp
TargetPointerResultTy TPR = Device.getMappingInfo().getTgtPtrBegin(
    HstPtrBegin, ArgSize, /*UpdateRefCount=*/false,
    /*UseHoldRefCount=*/false, /*MustContain=*/true);
```

随后不存在则 no-op（`present` 时报错），主机指针则 no-op（统一内存），其余按方向搬运：

```cpp
// omptarget.cpp:1416-1419  (TO)
if (ArgType & OMP_TGT_MAPTYPE_TO) {
  ...
  int Ret = Device.submitData(TgtPtrBegin, HstPtrBegin, ArgSize, AsyncInfo,
                              TPR.getEntry());
```

```cpp
// omptarget.cpp:1457-1461  (FROM)
if (ArgType & OMP_TGT_MAPTYPE_FROM) {
  ...
  int Ret = Device.retrieveData(HstPtrBegin, TgtPtrBegin, ArgSize, AsyncInfo,
                                TPR.getEntry());
```

对比 begin/end，update 的关键特征是：**没有任何 `incRefCount`/`decRefCount`，没有 `allocData`/`deleteData`**。FROM 搬运后还会挂一个后处理来还原主机影子指针（[omptarget.cpp:1470-1498](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1470-L1498)），但绝不动映射表本身。

非连续（如 Fortran 数组的跨步切片）走 `targetDataNonContiguous`（[omptarget.cpp:1505-1536](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1505-L1536)），它递归地把多维非连续描述展开成一连串连续段，每段最终也调用 `targetDataContiguous`。

#### 4.4.4 代码实践

**实践目标**：体会 update 不改引用计数。

**操作步骤**：

1. 阅读并对比三处 `getTgtPtrBegin`/`getTargetPointer` 调用：
   - begin：[omptarget.cpp:664-668](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L664-L668)（`UpdateRef` 由 `MEMBER_OF` 决定）
   - end：[omptarget.cpp:1135-1137](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1135-L1137)（`UpdateRef` 含 `PTR_AND_OBJ`）
   - update：[omptarget.cpp:1394-1396](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1394-L1396)（恒为 `false`，且 `MustContain=true`）
2. 列表记录每个调用传入的 `UpdateRefCount` 与 `MustContain`。

**需要观察的现象**：只有 update 把 `UpdateRefCount` 写死为 `false`，并把 `MustContain` 写死为 `true`；这正是「只搬不动映射」在参数层面的体现。

**预期结果**：能用一句话概括三者在「是否碰计数」「是否允许延伸命中」上的差异。

#### 4.4.5 小练习与答案

**练习 1**：对一个**尚未映射**的变量执行 `target update to(x)` 会怎样？

> **答案**：`getTgtPtrBegin` 因 `MustContain=true` 且不存在映射而返回 `IsPresent=false`，`targetDataContiguous` 把它当作 no-op 跳过（[omptarget.cpp:1398-1408](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1398-L1408)）；若带 `present` 修饰符则报错。它绝不会去分配——因为 update 不负责建立映射。

**练习 2**：`target update` 与 `target data map(tofrom:)` 在搬运方向上有何异同？

> **答案**：方向相同（都可 H2D/D2H）。区别在于 `map(tofrom:)` 在 begin 时建映射并 +1 计数、退出时 -1 并可能释放；`target update` 只在既有映射上做单向搬运，不改计数、不建不删。

---

### 4.5 `targetDataMapper`：用户自定义 mapper 的递归展开

#### 4.5.1 概念说明

OpenMP 允许用 `declare mapper` 为自定义类型定义「如何拆解映射」。例如把一个含指针的结构体拆成若干 `to`/`from` 段。运行时遇到带 mapper 的条目时，不能直接走常规路径，而是要先调用编译器生成的 mapper 函数把它展开成一族普通条目，再交给对应的 `targetData*` 函数处理。

#### 4.5.2 核心流程

```
targetDataMapper(Device, ArgBase, Arg, ..., ArgMapper, TargetDataFunction):
  1. 构造空的 MapperComponents
  2. 调用 (*ArgMapper)(&MapperComponents, ...)   # 编译器生成的函数填表
  3. 把 MapperComponents 拷贝成 args/args_base/sizes/types 数组
  4. 调用 TargetDataFunction(..., FromMapper=true)  # 递归回 begin/end/update
```

注意第 4 步：mapper 展开后**回调的正是同一个 `TargetDataFunction`**（begin/end/update 之一），只是带上 `FromMapper=true` 标志，用于区分「来自 mapper 递归」的调用（影响 PTR_AND_OBJ 的引用计数处理，见 [omptarget.cpp:657-658](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L657-L658)）。

#### 4.5.3 源码精读

`targetDataMapper` 的核心是「调用 mapper 函数填表 → 重建数组 → 回调」：

```cpp
// omptarget.cpp:307-311
MapperComponentsTy MapperComponents;
MapperFuncPtrTy MapperFuncPtr = (MapperFuncPtrTy)(ArgMapper);
(*MapperFuncPtr)((void *)&MapperComponents, ArgBase, Arg, ArgSize, ArgType,
                 ArgNames);
```

随后把展开出的每个 `MapComponentInfoTy` 拷进新数组，并以 `FromMapper=true` 回调：

```cpp
// omptarget.cpp:331-335
int Rc = TargetDataFunction(Loc, Device, MapperComponents.Components.size(),
                            MapperArgsBase.data(), MapperArgs.data(),
                            MapperArgSizes.data(), MapperArgTypes.data(),
                            MapperArgNames.data(), /*arg_mappers*/ nullptr,
                            AsyncInfo, StateInfo, /*FromMapper=*/true);
```

三个 `targetData*` 函数在主循环开头都先检查 `ArgMappers[I]`，若有 mapper 则改走 `targetDataMapper`（例如 begin 的 [omptarget.cpp:531-551](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L531-L551)，end 的 [omptarget.cpp:1103-1123](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1103-L1123)，update 的 [omptarget.cpp:1560-1579](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1560-L1579)）。这使 mapper 成为贯穿三阶段的「预处理插件」。

#### 4.5.4 代码实践

**实践目标**：确认 mapper 展开后回调的是同一个 `targetData*` 函数。

**操作步骤**：

1. 阅读 [`targetDataMapper` 全函数](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L300-L338)（omptarget.cpp:300-338）。
2. 注意它把传入的 `TargetDataFunction` 指针原样作为第 4 步的调用目标。
3. 回看 begin/end/update 主循环里对 `ArgMappers[I]` 的判断，确认三者都把自身作为 `TargetDataFunction` 传给 `targetDataMapper`。

**预期结果**：能画出「begin → 发现有 mapper → targetDataMapper → 再次 begin（FromMapper=true）」的递归调用链。

#### 4.5.5 小练习与答案

**练习 1**：`FromMapper=true` 这个标志为什么需要？

> **答案**：mapper 展开后，原条目与展开出的子条目之间会有 PTR_AND_OBJ 的引用计数归属问题。`FromMapper` 让 begin 在 [omptarget.cpp:657-658](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L657-L658) 正确决定是否对第一个子元素的 pointee 计数——避免 mapper 展开导致重复计数。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「端到端」的源码追踪。

**任务**：给定下面这段程序，分别预测并验证 begin / kernel / end 三个阶段的引用计数与搬运行为，并把每一步对应到源码位置。

```c
// walk.c —— 示例代码
#include <stdio.h>
int main(void) {
  int d[2] = {1, 2};
  int r = 0;
  #pragma omp target data map(to: d) map(from: r)   // 阶段① begin
  {
    #pragma omp target map(to: d) map(from: r)       // 阶段② begin/kernel/end
    {
      r = d[0] + d[1];
    }
  }                                                  // 阶段③ end
  printf("r=%d\n", r);
  return 0;
}
```

**步骤**：

1. **先预测**（只看源码，不运行）：
   - 阶段①：`d` 是新条目 → 分配 + refcount=1 + H2D；`r` 是新条目 → 分配 + refcount=1（`from` 不在 begin 搬运）。
   - 阶段②进入：`d` 已存在 → refcount 升到 2，**非新条目且非 always → 不搬**；`r` 已存在 → refcount 升到 2。
   - 阶段②退出：`d`/`r` 各 -1，回到 1，`IsLast=false` → **不删除、不回传**。
   - 阶段③：`d` -1 → 0（不 `from`，不回传，仅删除）；`r` -1 → 0 且 `from` → **D2H 回传**后延迟删除。
2. **再验证**：`LIBOMPTARGET_INFO=63 ./walk`，在日志中找到对应的 `Creating new map entry`（[Mapping.cpp:307](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L307)）、`Mapping exists ... (incremented)` / `(decremented)` / `(decremented, delayed deletion)`（[Mapping.cpp:245-251](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L245-L251) 与 468–473）、`Moving N bytes (tgt:..) -> (hst:..)`（[omptarget.cpp:1227](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1227)）等行，与预测逐一对照。
3. **画链路图**：把 `__tgt_target_data_begin_mapper` → `targetData` → `targetDataBegin` → `getTargetPointer` → `submitData` 这条链画出来，并标注 begin/end/update 各自在哪一步改计数、哪一步搬运、哪一步删除。

**预期结果**：能说清「为什么 `d` 在阶段②不再搬运、`r` 直到阶段③才回传」这两个现象，且都能在源码中指到具体行。**待本地验证**（日志细节以本机版本为准）。

## 6. 本讲小结

- 三个阶段共用同一套外壳 `targetData<>` 模板（[interface.cpp:114-194](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L114-L194)），差别只在传入的内部函数指针；`StateInfoTy` 仅 begin/end 分配。
- `targetDataBegin` 正序遍历，跳过 `LITERAL`/`PRIVATE`，延迟 `ATTACH`，调用 `getTargetPointer` 完成「+1 计数 + 可能 H2D」；H2D 触发条件是 `to && (新条目|always|本构造新分配)`。
- `targetDataEnd` 逆序遍历，调用 `getTgtPtrBegin` 完成「-1 计数 + IsLast 预测」；FROM 搬运触发条件是 `from && (IsLast|always|曾被释放)`，未触发者记为 skipped 待补救。
- 设备内存删除**推迟**到 `AsyncInfo.synchronize()` 之后的 `postProcessingTargetDataEnd`，由 `eraseMapEntry` + `deallocTgtPtrAndEntry` 完成，避免与在途搬运竞争。
- `targetDataUpdate` 用 `UpdateRefCount=false`、`MustContain=true` 查映射，只做 `submitData`/`retrieveData`，**不分配、不删除、不改计数**。
- `targetDataMapper` 把用户自定义 mapper 展开成一族普通条目，再回调同一个 `targetData*` 函数（`FromMapper=true`），是贯穿三阶段的预处理插件。

## 7. 下一步学习建议

本讲讲清了「数据搬运与计数增减的指挥层」。接下来：

1. **[u2-l6 内核启动流程](u2-l6-kernel-launch-flow.md)**：内核启动前后的数据准备正是复用了本讲的 `targetDataBegin`/`targetDataEnd`——它们被 `processDataBefore`（[omptarget.cpp:2055-2234](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2055-L2234)）与 `processDataAfter`（[omptarget.cpp:2238-2277](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2238-L2277)）调用。读完本讲再去看 `target()` 函数（[omptarget.cpp:2286-2392](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L2286-L2392)）会非常顺。
2. **[u2-l7 异步执行与 AsyncInfoTy](u2-l7-async-model.md)**：本讲反复出现的 `AsyncInfo.synchronize()` 与「后处理队列」正是异步模型的核心，建议紧接着学。
3. 想深入「指针附着（ATTACH）」全貌的读者，可继续精读 [`processAttachEntries`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L818-L958) 与 [`performPointerAttachment`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L447-L513)，它解释了「设备指针如何被绑定到设备 pointee」。
