# 通用插件接口 GenericPluginTy / GenericDeviceTy

## 1. 本讲目标

本讲是「插件架构、扩展机制与新 API」单元的第一讲，目标是打开 `plugins-nextgen/common/` 这个**通用插件框架**的内部结构，让你看懂「上层 libomptarget」与「底层各硬件后端（host / CUDA / AMDGPU / Level Zero）」之间那一层统一的 C++ 抽象是怎么设计的。

读完本讲，你应当能够：

1. 说出 nextgen 插件框架中 **GenericPluginTy / GenericDeviceTy / GenericKernelTy / PluginContextTy** 这几个核心基类各自的职责边界。
2. 看懂框架反复使用的**模板方法模式（Template Method）**：一个非虚的 `xxx()` 外壳负责公共逻辑，插件只需 override 一个纯虚的 `xxxImpl()`。
3. 区分**三层方法**：上层 `DeviceTy`（用户面）→ 插件的 `int32_t xxx()`（C 边界）→ 设备的 `Error xxx()`（框架外壳）→ 纯虚 `xxxImpl()`（后端实现）。
4. 理解框架以 **`llvm::Error` / `llvm::Expected`** 作为统一的错误传播通道，并在最外层把错误降级成 `OFFLOAD_SUCCESS / OFFLOAD_FAIL`。
5. 列出**新增一个插件需要 override 的关键虚函数**，并能解释 `GenericDeviceTy` 与上层 `DeviceTy` 各自负责什么。

## 2. 前置知识

本讲假设你已经学完第二单元的以下内容（这里只做一句话回顾，不重复）：

- **u2-l2 PluginManager**：进程级全局单例 `PM` 持有若干 `GenericPluginTy`，把所有插件展平成一组从 0 连续编号的 `DeviceTy`；存在「用户面 `UserId`」与「插件内 `DeviceId`」两套设备号。
- **u2-l3 DeviceTy**：`DeviceTy` 是「上层 OpenMP 逻辑」与「底层插件」之间的**外观层（Facade）**，自身不碰硬件，只把请求转发成 `RTL->某方法(RTLDeviceID, ...)`；它的成员 `RTL` 就是一个 `GenericPluginTy *`。

因此本讲的起点是：上层 `DeviceTy` 通过 `RTL->` 调用的那一组函数，到底落在 `GenericPluginTy` 的哪里，又是如何一层层下钻到具体硬件后端的。

补充几个本讲会用到的 C++ / LLVM 概念：

- **模板方法模式（Template Method）**：父类用一个**非虚**的 `f()` 定义算法骨架（固定调用顺序），在骨架里调用若干**纯虚**的 `fImpl()`；子类只填空 `fImpl()`，骨架本身不可改。本框架几乎每一个对外能力都是这种「外壳 + Impl」结构。
- **`llvm::Error` / `llvm::Expected<T>`**：LLVM 的错误处理类型。`Error` 是「要么成功（`Error::success()`），要么携带一个错误对象」的所有权指针；`Expected<T>` 是「要么是值 `T`，要么是 `Error`」。它们**必须被检查**（`if (auto Err = ...)` 或 `.takeError()`），否则程序会在析构时 abort，从而强制开发者处理错误。
- **纯虚函数（`= 0`）**：声明为 `virtual Ret foo(...) = 0;` 的函数没有默认实现，抽象类不能被实例化，子类必须 override 它。这是框架要求「每个后端必须实现」的契约表达。

## 3. 本讲源码地图

本讲只聚焦通用框架层，涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [plugins-nextgen/common/include/PluginInterface.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h) | 所有基类的声明：`AsyncInfoWrapperTy`、`GenericKernelTy`、`PluginContextTy`、`GenericDeviceTy`、`GenericPluginTy`、`GenericDeviceResourceManagerTy`，以及大量「外壳 + `Impl`」的虚函数契约。 |
| [plugins-nextgen/common/src/PluginInterface.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp) | 外壳函数的实现：公共逻辑（初始化、JIT、RPC、信息打印、错误降级、AsyncInfo 包装）都在这里。 |
| [plugins-nextgen/common/include/OffloadError.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/OffloadError.h) | 统一错误模型：`error::ErrorCode` 枚举、`OffloadError` 类与 `createOffloadError` 工厂。 |
| [include/device.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h) | 上层 `DeviceTy`，其成员 `RTL`（`GenericPluginTy *`）就是本讲框架的入口。 |
| [plugins-nextgen/cuda/src/rtl.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/cuda/src/rtl.cpp) | 一个真实后端，用于在「实践」里印证哪些虚函数被 override。 |

记住一句话定位：**`PluginInterface.h` 是 nextgen 插件框架的「合同范本」，每个 GPU/CPU 后端都是在这份范本上填空。**

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块。建议按顺序读，因为它们是层层嵌套的：先讲贯穿全局的「三层方法」骨架，再分别打开插件级、设备级两类基类，最后看异步适配器与错误处理两个横切机制。

### 4.1 三层方法、模板方法模式与上下层边界

#### 4.1.1 概念说明

整个 nextgen 框架最核心的设计就一句话：**公共逻辑上移到基类，硬件差异下沉到纯虚函数**。

为了同时服务两类调用者，框架对每一个能力都提供了**两个名字、三个层次**：

1. **C 边界函数（`int32_t`，蛇形命名，位于 `GenericPluginTy`）**：例如 `data_submit_async`、`launch_kernel`、`synchronize`。返回值只有 `OFFLOAD_SUCCESS / OFFLOAD_FAIL` 两个整数。这是给上层 `DeviceTy` 通过 `RTL->` 调用的稳定 C ABI。
2. **框架外壳（`Error` / `Expected<T>`，驼峰命名，位于 `GenericDeviceTy`）**：例如 `dataSubmit`、`launchKernel`。它做所有与具体硬件无关的公共工作（构造异步包装器、打印日志、收集 OMPT 回调、回收资源），然后调用纯虚 `...Impl()`。
3. **纯虚实现（`= 0`，位于具体后端）**：例如 CUDA 的 `CUDADeviceTy::dataSubmitImpl`。只负责「把请求翻译成厂商运行时 API（如 `cuMemcpyHtoDAsync`）」。

这就是经典的**模板方法模式**：`dataSubmit`（非虚）定义骨架，`dataSubmitImpl`（纯虚）是骨架里留给子类填的空位。

#### 4.1.2 核心流程

以「主机→设备拷贝」为例，一次调用的纵向穿越如下：

```
上层 libomptarget
   │  DeviceTy::submitData(...)              // 用户面 UserId
   ▼
RTL->data_submit_async(RTLDeviceID, ...)      // ① int32_t C 边界 (GenericPluginTy)
   │  取 getDevice(DeviceId)，错误降级为 OFFLOAD_FAIL
   ▼
GenericDeviceTy::dataSubmit(...)              // ② Error 外壳
   │  构造 AsyncInfoWrapperTy → 调 Impl → finalize
   ▼
GenericDeviceTy::dataSubmitImpl(...) = 0      // ③ 纯虚
   │  （由 CUDADeviceTy 等子类实现）
   ▼
厂商运行时 API（cuMemcpyHtoDAsync / ...）     // 真正碰硬件
```

关键点：**UserId → RTLDeviceID 的转换发生在第①层**（`GenericPluginTy::data_submit_async` 用 `getDevice(DeviceId)` 取插件内设备），**所有公共逻辑集中在第②层**，**硬件差异隔离在第③层**。

#### 4.1.3 源码精读

上层 `DeviceTy` 持有一个 `GenericPluginTy *` 作为 `RTL`，并直接调用蛇形命名函数——这就是框架的真正入口：

[include/device.h:39](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h#L39) 把 `GenericPluginTy` 引入为 `RTL` 的类型别名；[include/device.h:49](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h#L49) 声明成员 `GenericPluginTy *RTL;`。

[libomptarget/device.cpp:286](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L286) 与 [:362](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L362) 证实了这条转发：`DeviceTy` 把请求一行转给 `RTL->data_submit_async(...)`、`RTL->launch_kernel(...)`。

第①层 C 边界函数把 `Error` 降级成整数，见 [PluginInterface.cpp:1663-1675](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1663-L1675)（`data_submit_async`）：

```cpp
int32_t GenericPluginTy::data_submit_async(int32_t DeviceId, void *TgtPtr,
                                           void *HstPtr, int64_t Size,
                                           __tgt_async_info *AsyncInfoPtr) {
  auto Err = getDevice(DeviceId).dataSubmit(TgtPtr, HstPtr, Size, AsyncInfoPtr);
  if (Err) {
    REPORT() << "Failure to copy data ... " << toString(std::move(Err));
    return OFFLOAD_FAIL;
  }
  return OFFLOAD_SUCCESS;
}
```

第②层外壳函数只做「构造异步包装器 → 调 Impl → 收尾」，见 [PluginInterface.cpp:1132-1139](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1132-L1139)：

```cpp
Error GenericDeviceTy::dataSubmit(void *TgtPtr, const void *HstPtr,
                                  int64_t Size, __tgt_async_info *AsyncInfo) {
  AsyncInfoWrapperTy AsyncInfoWrapper(*this, AsyncInfo);   // 公共逻辑
  auto Err = dataSubmitImpl(TgtPtr, HstPtr, Size, AsyncInfoWrapper); // 纯虚
  AsyncInfoWrapper.finalize(Err);                          // 公共逻辑
  return Err;
}
```

第③层纯虚契约在头文件里声明，见 [PluginInterface.h:1058-1062](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1058-L1062)：`dataSubmit`（非虚，有体）与 `dataSubmitImpl(...) = 0`（纯虚）成对出现。

#### 4.1.4 代码实践

**实践目标**：在源码层面追踪「一条数据拷贝请求的纵向穿越」，确认三层方法的边界。

**操作步骤**：

1. 打开 `libomptarget/device.cpp`，定位 `DeviceTy::submitData`（搜索 `data_submit_async`），确认它只是 `return RTL->data_submit_async(...)`。
2. 打开 `plugins-nextgen/common/src/PluginInterface.cpp`，看 `GenericPluginTy::data_submit_async`（1663 行起）如何调用 `getDevice(DeviceId).dataSubmit(...)`。
3. 跳到同文件 `GenericDeviceTy::dataSubmit`（1132 行起），确认它构造 `AsyncInfoWrapperTy` 后调用 `dataSubmitImpl`。
4. 打开 `plugins-nextgen/common/include/PluginInterface.h`，确认 `dataSubmitImpl(...) = 0` 是纯虚。
5. 打开 `plugins-nextgen/cuda/src/rtl.cpp`，搜索 `dataSubmitImpl`，确认 CUDA 在这里调用 `cuMemcpyHtoDAsync`（具体函数名以源码为准）。

**需要观察的现象**：每一层都只「多做了点公共工作 + 转发」，真正碰硬件的代码只在最底层后端。

**预期结果**：你应当画出一张与 4.1.2 一致的纵向流程图，并能指出「哪一行做 UserId→RTLDeviceID 转换、哪一行做错误降级、哪一行是纯虚分派」。

**运行结果**：待本地验证（本实践为源码阅读型，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么框架要把 `dataSubmit` 设计成非虚、而把 `dataSubmitImpl` 设计成纯虚？如果把公共逻辑直接写进每个后端的 `dataSubmitImpl` 会怎样？

**参考答案**：非虚的 `dataSubmit` 是模板方法模式的骨架，它把「构造 `AsyncInfoWrapperTy`、`finalize` 收尾」等所有后端都要做的公共逻辑集中到一处，避免在每个后端重复。若把公共逻辑搬进每个 `dataSubmitImpl`，则 N 个后端要复制 N 份相同代码，且修改公共行为时极易遗漏，违反 DRY；纯虚 `dataSubmitImpl` 只暴露「真正因硬件而异」的最小切面。

**练习 2**：`GenericPluginTy::data_submit_async` 返回 `int32_t`，而 `GenericDeviceTy::dataSubmit` 返回 `Error`。这两层返回类型不一致是为什么？

**参考答案**：上层 `DeviceTy` 通过 C ABI 调用插件，需要一个不抛异常、跨编译单元稳定的整数状态码（`OFFLOAD_SUCCESS/FAIL`）；而框架内部用 `llvm::Error` 才能携带结构化的错误信息（`ErrorCode` + 文案），并靠「Error 必须被检查」的机制强制处理。C 边界层负责把 `Error` 转换成整数并 `REPORT` 出文案，是两种风格的衔接点。

---

### 4.2 GenericPluginTy：插件级抽象

#### 4.2.1 概念说明

`GenericPluginTy` 代表**一个硬件后端（一个 `.so` 插件）**，例如「整个 CUDA 后端」或「整个 Level Zero 后端」。它持有：

- 该后端能提供的**设备数组** `Devices`（`GenericDeviceTy *`）；
- 与后端绑定的若干**公共设施**：全局变量处理器 `GlobalHandler`、JIT 引擎 `JIT`、RPC 服务器 `RPCServer`、内存分配器 `Allocator`；
- 设备号映射表 `UserDeviceIds`（插件内 `DeviceId` ↔ 用户面 `UserId`）。

它对外暴露两类成员（这与 4.1 的三层方法对应）：

- **蛇形命名的 C 边界函数**（`data_alloc`、`launch_kernel`、`synchronize`、`isPluginCompatible` 等），供上层 `DeviceTy` 调用；
- **供子类 override 的纯虚契约**（`initImpl`、`createDevice`、`getMagicElfBits`、`getTripleArch` 等）。

#### 4.2.2 核心流程

插件的生命周期是「构造 → 探测硬件 → 逐个初始化设备 → 服务 → 逐个销毁设备 → 销毁」：

```
GenericPluginTy(Triple::ArchType)        // 构造，仅记架构、置空设施
   │
init()                                    // ① 探测硬件
   ├─ initImpl()  → 返回设备数 NumDevices   //   纯虚，由后端实现
   ├─ Devices.resize(NumDevices)            //   预留指针槽位（全 nullptr）
   ├─ createGlobalHandler()                 //   纯虚
   └─ new RPCServerTy(*this)
   │
initDevice(DeviceId)                       // ② 首次用到某设备时
   ├─ createDevice(*this, DeviceId, NumDevices)  // 纯虚：new CUDADeviceTy{...}
   ├─ Devices[DeviceId] = Device
   └─ Device->init(*this)                   //   触发 4.3 的设备初始化
   │
... 服务期：上层经 C 边界函数访问设备 ...
   │
deinitDevice(DeviceId) → Device->deinit() → delete Device
deinit() → 逐设备 deinitDevice → delete GlobalHandler/RPCServer → deinitImpl()
```

注意「构造 ≠ 初始化」与「按需初始化设备」两个细节：构造函数只记架构、不碰硬件；`init()` 才真正探测硬件、得到设备数；单个设备的初始化进一步推迟到 `initDevice`，体现懒加载。

#### 4.2.3 源码精读

`GenericPluginTy` 的声明与构造见 [PluginInterface.h:1462-1466](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1462-L1466)——构造仅 `GlobalHandler(nullptr), JIT(TA), RPCServer(nullptr)`，确实不碰硬件。

四组关键纯虚契约（插件必须 override）：

- `initImpl()` 返回设备数：[PluginInterface.h:1473-1474](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1473-L1474)
- `createDevice(...)` / `createGlobalHandler()`：[PluginInterface.h:1481-1486](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1481-L1486)（工厂方法，由后端 `new` 出具体子类）
- `getMagicElfBits()` / `getTripleArch()` / `getName()`：[PluginInterface.h:1517-1523](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1517-L1523)（声明本插件识别哪种 ELF、对应哪个目标三元组）

`init()` 外壳把「探测硬件 + 预留设备槽 + 创建公共设施」串起来，见 [PluginInterface.cpp:1326-1349](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1326-L1349)：

```cpp
Error GenericPluginTy::init() {
  if (Initialized) return Plugin::success();
  auto NumDevicesOrErr = initImpl();          // 纯虚：探测硬件
  ...
  NumDevices = *NumDevicesOrErr;
  if (NumDevices == 0) return Plugin::success();
  Devices.resize(NumDevices, nullptr);        // 预留槽位，暂不构造
  GlobalHandler = createGlobalHandler();      // 纯虚工厂
  RPCServer = new RPCServerTy(*this);
  return Plugin::success();
}
```

`initDevice()` 把单个设备的创建与初始化合在一起，见 [PluginInterface.cpp:1381-1393](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1381-L1393)：先 `createDevice(...)`（纯虚），再 `Device->init(*this)`（设备级外壳，见 4.3）。

镜像兼容性判断是「插件级」与「设备级」两层：`isPluginCompatible` 在设备初始化前就能判断镜像是否属于本插件（按 ELF magic / 三元组），见 [PluginInterface.cpp:1440-1471](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1440-L1471)；`isDeviceCompatible` 在此基础上再调纯虚 `isELFCompatible(DeviceId, Image)` 做架构细判（如 sm_80），见 [:1473-1511](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1473-L1511)。这正是 u2-l1 所述「插件级 + 设备级双层兼容判断」的落点。

`PluginContextTy`（[PluginInterface.h:865-883](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L865-L883)）是一组设备的轻量上下文容器，默认实现只持有 `(Plugin, Devices)`。需要原生上下文状态的后端（如 Level Zero 的 driver/context）可 override `createPluginContext`（[PluginInterface.h:1621-1624](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1621-L1624)）返回自己的子类。

#### 4.2.4 代码实践

**实践目标**：用一个真实后端印证「插件级需要 override 哪些纯虚函数」。

**操作步骤**：

1. 打开 `plugins-nextgen/cuda/src/rtl.cpp`，定位 `struct CUDAPluginTy final : public GenericPluginTy`（约 1665 行）。
2. 在该结构体内列出它 override 的方法：`initImpl`（约 1674）、`deinitImpl`（约 1721）、`createDevice`（约 1724）、`getMagicElfBits`（约 1735，返回 `ELF::EM_CUDA`）、`getTripleArch`（约 1737），以及 `createGlobalHandler` / `getName`。
3. 对照本节列出的「插件必须 override」清单，逐条打勾。

**需要观察的现象**：CUDA 插件 override 的方法，恰好就是 `GenericPluginTy` 里 `= 0` 的那几个；它**没有**重写 `init / deinit / initDevice` 这些外壳。

**预期结果**：你会得到一份「CUDA 插件填空清单」，并与 host / amdgpu 插件对比，发现大家填的是同一组虚函数。

**运行结果**：待本地验证（源码阅读型实践）。

#### 4.2.5 小练习与答案

**练习 1**：`GenericPluginTy::init()` 为什么在 `initImpl()` 返回 `NumDevices == 0` 时直接 `return`、不再创建 `GlobalHandler`？

**参考答案**：`getNumDevices() == 0` 意味着本机没有该类硬件（例如未装 NVIDIA 驱动），插件没有可用设备。此时不创建 `GlobalHandler`、不 `resize Devices`，可以避免无意义的资源分配；`deinit()` 也据此用 `if (GlobalHandler)` 守护（见 [PluginInterface.cpp:1364](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1364)）。这是一种「无设备即无设施」的省内存设计。

**练习 2**：`isPluginCompatible` 与 `isDeviceCompatible` 都存在，为什么不合并成一个？

**参考答案**：因为镜像可能在「任何设备被初始化之前」就需要判断归属（u2-l1 的注册期登记阶段）。此时设备尚未 `initDevice`，无法做架构细判；所以拆成两步：插件级用 ELF magic + 三元组（`getMagicElfBits`/`getTripleArch`）粗判，不依赖设备；设备级在设备可用后再调纯虚 `isELFCompatible(DeviceId, ...)` 做细判（如具体 compute capability）。两层分别对应「能不能交给这个插件」与「能不能在这个具体设备上跑」。

---

### 4.3 GenericDeviceTy：设备级抽象

#### 4.3.1 概念说明

如果说 `GenericPluginTy` 是「一个后端」，那 `GenericDeviceTy` 就是「一张卡（一个设备）」。它是 nextgen 框架里**最庞大**的基类，因为几乎所有与硬件交互的能力都挂在这里：内存分配/释放、数据搬运（H2D/D2H/D2D）、内核启动、事件、同步、设备信息查询、全局变量读写等。

它持有：

- 反向引用 `Plugin`（所属 `GenericPluginTy`）；
- 设备内编号 `DeviceId` 与唯一标识 `DeviceUid`；
- 硬件网格参数 `GridValues`（warp 大小、最大线程/团队数等）；
- 已加载镜像列表 `LoadedImages`、内存管理器 `MemoryManager`、RPC 服务器指针、Record/Replay 管理器等。

与 `GenericPluginTy` 一样，它的每个能力都是「外壳 + 纯虚 `Impl`」结构，公共逻辑（异步包装、日志、JIT、OMPT 回调）都在外壳里。

#### 4.3.2 核心流程

设备的「内核启动」最能体现外壳做了多少公共工作。`launchKernel`（外壳）→ `GenericKernelTy::launch`（公共编排）→ `launchImpl`（纯虚）的过程：

```
GenericDeviceTy::launchKernel(EntryPtr, Args, KernelArgs, AsyncInfo)
   ├─ 构造 AsyncInfoWrapperTy
   ├─ 记录 kernel 启动栈（若开启 trace）
   ├─ GenericKernel.launch(...)            // 见 4.3.3
   │     ├─ 计算 effective NumThreads/NumBlocks
   │     ├─ prepareBlockMemory (动态共享内存，含 fallback)
   │     ├─ getKernelLaunchEnvironment (reduction buffer 等)
   │     ├─ prepareArgs (参数 + dyn_ptr)
   │     ├─ printLaunchInfo (LIBOMPTARGET_INFO)
   │     └─ launchImpl(...) = 0            // 纯虚：后端真正启动内核
   └─ AsyncInfoWrapper.finalize(Err)       // 同步/异步收尾
```

设备初始化外壳 `init()` 同样在纯虚 `initImpl()` 前后做大量公共工作（OMPT `device_initialize` 回调、读取并应用栈/堆大小环境变量、根据 `OMP_NumTeams` 收紧 `GridValues`、按需创建内存管理器），见 4.3.3。

#### 4.3.3 源码精读

类声明与构造见 [PluginInterface.h:888-892](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L888-L892)，构造函数把 `Plugin`、`DeviceId`、`GridValues` 存好，并解析一批 `OMP_*` 环境变量（[PluginInterface.cpp:490-496](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L490-L496)）。

几对典型的「外壳 + 纯虚」契约（都在头文件里成对声明）：

| 能力 | 非虚外壳（公共逻辑） | 纯虚 Impl（后端实现） |
| --- | --- | --- |
| 初始化/去初始化 | `init` / `deinit` | `initImpl` / `deinitImpl` = 0 |
| 加载镜像 | `loadBinary` | `loadBinaryImpl` = 0 |
| 数据搬运 | `dataSubmit` / `dataRetrieve` / `dataExchange` | `dataSubmitImpl` / `dataRetrieveImpl` / `dataExchangeImpl` = 0 |
| 同步 | `synchronize` / `queryAsync` | `synchronizeImpl` / `queryAsyncImpl` = 0 |
| 内核启动 | `launchKernel`（外壳）→ `GenericKernelTy::launch`（编排） | `GenericKernelTy::launchImpl` = 0 |
| 事件 | `createEvent` / `recordEvent` / `syncEvent` ... | 对应 `...Impl` = 0 |
| 设置上下文 | — | `setContext` = 0 |

`init()` 外壳是「公共逻辑有多少」的最佳例证，见 [PluginInterface.cpp:554-610](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L554-L610)：先调纯虚 `initImpl(Plugin)`，再发 OMPT 回调、读 `LIBOMPTARGET_STACK_SIZE/HEAP_SIZE` 环境变量并回调 setter、收紧 `GridValues`、按需创建 `MemoryManagerTy`。后端的 `initImpl` 只关心「怎么让这张卡就绪」，环境变量与回调全由外壳统一处理。

`loadBinary` 外壳展示了 JIT 与 RPC 两个横切机制如何挂在镜像加载链路上，见 [PluginInterface.cpp:673-734](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L673-L734)：先判断镜像是否为 bitcode（若是则 `Plugin.getJIT().process` 编译），再调纯虚 `loadBinaryImpl`，再 `setupRPCServer`，最后 `callGlobalConstructors`。后端的 `loadBinaryImpl` 只负责「把二进制解析成 `DeviceImageTy`」。

`launchKernel` 外壳见 [PluginInterface.cpp:1179-1207](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1179-L1207)：它把 `EntryPtr`（一个 `GenericKernelTy *`）重新解释，记录启动栈，转给 `GenericKernel.launch`，再 `finalize`。

内核的公共编排 `GenericKernelTy::launch` 见 [PluginInterface.cpp:246-348](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L246-L348)：算 effective 线程/块数 → `prepareBlockMemory`（含 fallback）→ `getKernelLaunchEnvironment`（reduction buffer）→ `prepareArgs`（含 dyn_ptr 处理）→ `printLaunchInfo` →（可选 Record/Replay prologue）→ 纯虚 `launchImpl` →（可选 Record/Replay epilogue）。`launchImpl` 的纯虚声明在 [PluginInterface.h:448-452](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L448-L452)。

> 提示：`GenericKernelTy` 是与 `GenericDeviceTy` 配套的第三个基类（[PluginInterface.h:430-611](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L430-L611)），每个后端用 `constructKernel(Name)`（纯虚，[:1269](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1269)）`new` 出自己的 kernel 子类。kernel 的生命周期与启动抽象会在 u3-l2 专门讲。

#### 4.3.4 代码实践

**实践目标**：列出「新增一个后端时，`GenericDeviceTy` 这一层至少要 override 哪些纯虚函数」。

**操作步骤**：

1. 在 `PluginInterface.h` 的 `GenericDeviceTy` 范围内（888–1457 行）搜索所有 `= 0`。
2. 把它们分成几组：初始化（`initImpl`/`deinitImpl`/`setContext`）、镜像（`loadBinaryImpl`/`unloadBinaryImpl`）、数据搬运（`dataSubmitImpl`/`dataRetrieveImpl`/`dataExchangeImpl`/`dataFence`/`dataLockImpl`/`dataUnlockImpl`/`isPinnedPtrImpl`）、同步（`synchronizeImpl`/`queryAsyncImpl`）、事件（`createEventImpl`/...）、内核与栈（`constructKernel`/`setDeviceStackSize`/`getDeviceStackSize`）、信息（`obtainInfoImpl`/`hasPendingWorkImpl`/`initAsyncInfoImpl`/`enqueueHostCallImpl`）。
3. 打开 `plugins-nextgen/cuda/src/rtl.cpp` 的 `CUDADeviceTy`（约 273 行起），核对它是否 override 了其中绝大多数（如 `initImpl` 约 282、`deinitImpl` 约 411、`loadBinaryImpl` 约 564、`dataSubmitImpl` 约 817、`dataRetrieveImpl` 约 831）。

**需要观察的现象**：CUDA 几乎为每一个 `= 0` 都提供了 override；而那些有默认实现（非纯虚）的能力（如 `dataPrefetchImpl`、虚拟地址管理），CUDA 才按需选择是否重写。

**预期结果**：得到一份「设备级必须 override 清单」，作为本讲综合实践（4.5）的输入。

**运行结果**：待本地验证（源码阅读型实践）。

#### 4.3.5 小练习与答案

**练习 1**：`GenericDeviceTy` 拥有内存管理器 `MemoryManager`，但又同时继承自 `DeviceAllocatorTy`。请结合 `init()` 说明二者的关系。

**参考答案**：`MemoryManagerTy` 是一个**可选的** per-device free-list 分配器（仅当 `LIBOMPTARGET_MEMORY_MANAGER_THRESHOLD` 启用时才在 `init()` 末尾创建，见 [PluginInterface.cpp:601-607](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L601-L607)）。而 `DeviceAllocatorTy` 是更底层的「分配接口」，由后端 override `allocate/free` 提供原生的 `cuMalloc`/`cuFree` 等。`dataAlloc` 外壳（[PluginInterface.cpp:1040 附近](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1040)）会优先走内存管理器的 free-list，未命中或未启用时回落到原生分配器。即 free-list 是原生分配之上的一层缓存。内存管理器细节见 u3-l6。

**练习 2**：上层 `DeviceTy` 已经是 Facade，为什么还要在 `GenericDeviceTy` 这一层再做一个「外壳 + Impl」？两层 Facade 是否冗余？

**参考答案**：不冗余，二者职责正交。上层 `DeviceTy`（libomptarget）解决的是「OpenMP 用户面语义」——维护 `UserId`/`RTLDeviceID` 双编号、内嵌 `MappingInfoTy` 做主机-设备指针映射、做 OMPT 上层回调、决定走哪个 `Kind` 的分配。它**不关心**具体硬件。`GenericDeviceTy`（插件框架）解决的则是「在不知道是哪张卡的前提下，提供所有后端都一致的公共编排」——异步包装、JIT、RPC、环境变量应用、共享内存/参数准备。下层各后端的 `xxxImpl` 才真正碰硬件。三层各管一段：用户语义 → 跨后端公共逻辑 → 单后端硬件细节。

---

### 4.4 AsyncInfoWrapperTy 与异步上下文适配

#### 4.4.1 概念说明

`AsyncInfoWrapperTy` 是 4.1 里反复出现的「异步包装器」。它解决一个具体问题：上层传入的 `__tgt_async_info *` **可能为空**（表示这次调用是同步的）。

如果为空，包装器就**内部维护一个 `LocalAsyncInfo`**，并在析构前的 `finalize()` 里**显式同步**——于是同一段后端代码（只认 `AsyncInfoWrapperTy`）既能跑异步、也能跑同步，后端无需写两份。

它还提供「按需懒初始化队列」`getOrInitQueue`，以及「同步完成后才释放的关联内存」`freeAllocationAfterSynchronization`（用于 reduction buffer、共享内存 fallback 等必须在内核跑完后才能释放的临时缓冲）。

#### 4.4.2 核心流程

每个数据搬运/内核启动外壳都遵循同一个三段式：

```
{  AsyncInfoWrapperTy Wrapper(*this, AsyncInfo);   // AsyncInfo 可能为 null
   auto Err = xxxImpl(..., Wrapper);               // 后端用 Wrapper.getOrInitQueue
   Wrapper.finalize(Err);                          // 若是 LocalAsyncInfo 则同步
}  // 析构 assert(!AsyncInfoPtr && "not finalized")
```

`finalize` 的判定逻辑：

- 若包装的是外部 `AsyncInfo`（异步场景）→ 不主动同步，把队列留给上层管理；
- 若包装的是 `LocalAsyncInfo`（同步场景）且队列非空且无错 → 调 `Device.synchronize(&LocalAsyncInfo)`，使本次调用整体表现为同步。

#### 4.4.3 源码精读

`AsyncInfoWrapperTy` 声明见 [PluginInterface.h:106-180](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L106-L180)：构造函数在 `AsyncInfoPtr` 为空时改用 `&LocalAsyncInfo`（[PluginInterface.cpp:45-50](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L45-L50)），析构 `assert(!AsyncInfoPtr && "not finalized")` 强制必须先 `finalize`。

`finalize` 的核心判定见 [PluginInterface.cpp:59-71](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L59-L71)：

```cpp
void AsyncInfoWrapperTy::finalize(Error &Err) {
  // 仅当用的是内部 LocalAsyncInfo 时，才显式同步 → 同步语义
  if (AsyncInfoPtr == &LocalAsyncInfo && LocalAsyncInfo.Queue && !Err)
    Err = Device.synchronize(&LocalAsyncInfo);
  AsyncInfoPtr = nullptr;   // 失效，允许析构
}
```

`synchronize()`（不释放队列的显式同步，供 Record/Replay 在内核前后等待）见 [PluginInterface.cpp:52-57](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L52-L57)。

队列懒初始化 `getOrInitQueue` 在头文件里（[PluginInterface.h:141-151](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L141-L151)）：它通过一个**资源管理器** `ResourceManager.getResource(...)` 从池里取/建队列。资源管理器 `GenericDeviceResourceManagerTy`（[PluginInterface.h:1865 起](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1865-L1909)）是一个泛型对象池（队列、事件等可复用资源都靠它），`getResource` 在池不够时会按下列方式扩容：

\[
\text{newSize} = \max\bigl(2\cdot\text{NextAvailable},\ \text{NextAvailable} + \text{Num}\bigr)
\]

即「翻倍或恰好够用，取大者」（见 [PluginInterface.h:1938-1942](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1938-L1942)），避免每次都向厂商运行时申请新流/事件。

#### 4.4.4 代码实践

**实践目标**：确认「同步调用其实是异步调用 + finalize 时强制同步」这一统一模型。

**操作步骤**：

1. 在 `PluginInterface.cpp` 搜索 `AsyncInfoWrapperTy AsyncInfoWrapper(*this, AsyncInfo);`，数一数有多少个外壳（`dataSubmit`/`dataRetrieve`/`launchKernel`/`initAsyncInfo`/...）用了同一段三段式。
2. 再看 `GenericPluginTy::data_submit`（同步版，[:1657](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1657)）如何通过传 `AsyncInfoPtr=nullptr` 复用 `data_submit_async`。
3. 结合 `finalize`，解释为什么传 `nullptr` 最终会变成同步。

**需要观察的现象**：同步入口只是把 `nullptr` 当 AsyncInfo 传下去；真正「同步化」发生在 `finalize` 检测到 `&LocalAsyncInfo` 时。

**预期结果**：能用一句话说清「框架没有为同步单独写一套代码，而是用 `LocalAsyncInfo` + `finalize` 把异步实现复用成同步」。

**运行结果**：待本地验证（源码阅读型实践）。

#### 4.4.5 小练习与答案

**练习 1**：`AsyncInfoWrapperTy` 的析构里有 `assert(!AsyncInfoPtr && "AsyncInfoWrapperTy not finalized")`。这个断言在防什么错误？

**参考答案**：它防止「创建了包装器却忘记调用 `finalize`」。因为若包装的是 `LocalAsyncInfo`，只有 `finalize` 才会触发同步；忘调 `finalize` 会导致这次「同步调用」实际没等内核跑完就返回，且队列里的异步操作可能悬空。断言把这种使用错误在 Debug 构建下提前暴露。

**练习 2**：`freeAllocationAfterSynchronization(Ptr)` 把指针挂到 `AssociatedAllocations` 列表上。为什么不直接 `free(Ptr)`？

**参考答案**：因为 `Ptr`（如 reduction buffer、共享内存 fallback 缓冲）正被**刚提交到队列、尚未跑完**的内核使用。立即释放会被厂商运行时拒绝或导致 use-after-free。挂在 `AssociatedAllocations` 上，等到 `finalize`/同步确认队列排空后才统一释放，保证释放发生在所有使用者完成之后。

---

### 4.5 基于 llvm::Error 的统一错误处理与设备信息模型

#### 4.5.1 概念说明

框架内部全部用 `llvm::Error` / `llvm::Expected<T>` 传播错误（见 4.1 的第②③层）。它基于一个自定义错误类型 `error::OffloadError`，携带一个 `error::ErrorCode` 枚举值（如 `INVALID_ARGUMENT`、`UNSUPPORTED`、`INVALID_BINARY`、`COMPILE_FAILURE`，定义在构建期生成的 `OffloadErrcodes.inc`）。

三个工厂函数让创建错误很简洁（集中在 `Plugin` 命名空间）：

- `Plugin::success()` → `Error::success()`；
- `Plugin::error(Code, "fmt", args...)` → 创建带错误码的 `OffloadError`；
- `Plugin::check(ErrorCode, "fmt")` → 把后端返回的整数错误码翻译成 `Error`（由各后端自行定义，TODO 注释说明）。

这套机制只在最外层（C 边界 `int32_t` 函数）降级成 `OFFLOAD_SUCCESS/FAIL`，并在降级前 `REPORT` 出错误文案——所以框架内部能精确传播错误原因，上层只看得到成败。

此外，`GenericDeviceTy::obtainInfo()` 用一个 `InfoTreeNode` 树来描述设备属性，既是 `printInfo`（给 `llvm-offload-device-info` 用）的数据源，也是 liboffload `olGetDeviceInfo` 查询的数据源。

#### 4.5.2 核心流程

错误的生命周期：

```
后端 xxxImpl 失败
   └─ return Plugin::error(ErrorCode::XXX, "描述 %s", ...);   // 创建 OffloadError
        │  Error 沿调用栈向上传播（每一层 if (Err) return Err）
        ▼
C 边界 int32_t 函数
   ├─ toString(std::move(Err)) → 字符串
   ├─ REPORT() << "Failure ... " << 字符串                  // 打印原因
   └─ return OFFLOAD_FAIL                                    // 降级
```

设备信息的生命周期：

```
GenericDeviceTy::printInfo() / obtainInfo()
   └─ obtainInfoImpl() = 0           // 后端填 InfoTreeNode
        ├─ 每条属性：root.add("Key", Value, "Units", DeviceInfo::KEY)
        └─ 返回树
   ▼
外壳补 "UID" 等；InfoTreeNode::print() 递归打印 / liboffload 按 DeviceInfo 枚举查询
```

#### 4.5.3 源码精读

错误模型定义见 [OffloadError.h:19-48](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/OffloadError.h#L19-L48)：`ErrorCode` 枚举（[:19-23](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/OffloadError.h#L19-L23)）由 X-macro `OFFLOAD_ERRC` 从 `OffloadErrcodes.inc` 生成；`OffloadError` 继承自 `llvm::StringError` 并绑定一个 `ErrorCode`；`createOffloadError` 工厂支持「文案 + 错误码」与「包装另一个 Error 并附 Context」两种形式（[:51-84](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/OffloadError.h#L51-L84)）。

`Plugin` 命名空间把创建错误的接口收敛到一处，见 [PluginInterface.h:72-104](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L72-L104)：`success()` / `error(...)` / `check(...)`。框架代码里随处可见 `return Plugin::error(ErrorCode::UNSUPPORTED, "...")`（如 [:903](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L903)、[:462](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L462)）。

错误降级点示例见 [PluginInterface.cpp:1663-1675](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1663-L1675)（`data_submit_async`）与 [:1723-1738](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1723-L1738)（`launch_kernel`）：都是 `if (Err) { REPORT() << ... << toString(std::move(Err)); return OFFLOAD_FAIL; }`。

设备信息模型见 [PluginInterface.h:182-316](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L182-L316)：`DeviceInfo` 枚举（[:182-186](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L182-L186)，由 `OffloadInfo.inc` 生成）标注每条属性对应的 liboffload 查询键；`InfoTreeNode` 是一个键值树，`add(Key, Value, Units, DeviceInfoKey)` 既把节点挂到子列表，又在 `DeviceInfoMap` 里登记枚举→下标映射（[:219-243](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L219-L243)）。`obtainInfo()` 外壳在后端填好的树根上补一个 `"UID"` 节点（[PluginInterface.cpp:1230-1235](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1230-L1235)），`printInfo()` 则递归对齐打印（[PluginInterface.h:264-315](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L264-L315)）。

#### 4.5.4 代码实践

**实践目标**：把「框架内的 `Error`」与「上层的整数状态码」对照清楚。

**操作步骤**：

1. 在 `PluginInterface.cpp` 里找一处 `Plugin::error(ErrorCode::..., "...")`（如 `loadBinary` 对空镜像的处理，[PluginInterface.cpp:683-685](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L683-L685)）。
2. 沿调用栈向上追：这个 `Error` 会经过 `GenericDeviceTy::loadBinary`（`Expected`）→ `GenericPluginTy::load_binary`（[:1552 起](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1552)）→ 上层 `DeviceTy::loadBinary`。
3. 观察在哪一层 `Error` 被转成 `OFFLOAD_FAIL` 并打印文案。
4.（可选）运行 `llvm-offload-device-info`，对照 `printInfo` 的缩进输出，反推 `InfoTreeNode` 的树结构。

**需要观察的现象**：错误原因（文案 + 错误码）在框架内部一路保留，直到最外层 C 边界才被消费成整数；`llvm::Error`「必须被检查」的特性使得任何中间层忘记处理都会在 Debug 下 abort。

**预期结果**：能指出「错误在哪里产生、在哪里降级、文案在哪里打印」三个位置。

**运行结果**：待本地验证（源码阅读型实践；`llvm-offload-device-info` 部分需本地有可用设备并已构建）。

#### 4.5.5 小练习与答案

**练习 1**：`Plugin::error(ErrorCode, Error &&OtherError, const char *Context)` 这个三参重载（[OffloadError.h:66-84](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/OffloadError.h#L66-L84)）有什么用？请结合 `loadBinary` 里的 JIT 失败处理说明。

**参考答案**：它用于「把一个已有错误包装进更高层语境」。例如 `loadBinary` 里 JIT 编译失败时（[PluginInterface.cpp:689-694](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L689-L694)）调 `Plugin::error(ErrorCode::COMPILE_FAILURE, CompiledImageOrErr.takeError(), "failure to jit IR image")`：把 JIT 引擎返回的内部错误当成「OtherError」，附上 Context 文案，统一标注成 `COMPILE_FAILURE`。这样既保留了原始错误信息，又给上层一个稳定的错误码。

**练习 2**：`InfoTreeNode` 为什么用「子节点是 vector」而不是「map」，且允许同一个 Key 出现多次？

**参考答案**：见头文件注释（[PluginInterface.h:201-205](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L201-L205)）：一是 Key 需要被拥有（`std::string`），二是**打印顺序很重要**（设备信息要按固定顺序展示），三是同一 Key 可能多次出现（例如多维网格的 x/y/z 三个分量都叫 "Range"）。`vector` 保序且允许重复，map 则三者都做不到。需要按 `DeviceInfo` 枚举快速查找时，另有 `DeviceInfoMap` 做索引。

## 5. 综合实践

把本讲 5 个模块串起来，完成下面这个**「为虚构后端画骨架」**的任务（纯源码阅读与设计型，不写真实代码）：

> 假设你要为某种新加速器「Foo」写一个 nextgen 插件。请基于本讲对 `PluginInterface.h` 的理解，产出三份清单。

**步骤 1 — 插件级清单（来自 4.2）**：列出 `FooPluginTy : public GenericPluginTy` 必须 override 的纯虚函数（至少 `initImpl`、`deinitImpl`、`createDevice`、`createGlobalHandler`、`getMagicElfBits`、`getTripleArch`、`getName`、`isELFCompatible`），并指出 `getMagicElfBits` / `getTripleArch` 对一个新后端意味着什么（识别哪种 ELF、声明哪个目标三元组）。

**步骤 2 — 设备级清单（来自 4.3）**：列出 `FooDeviceTy : public GenericDeviceTy` 至少要 override 的纯虚函数（参照 4.3.4 的分组：初始化、镜像、数据搬运、同步、事件、内核与栈、信息），并标注哪些能力有默认实现可以暂不 override（如 `dataPrefetchImpl`、虚拟地址管理）。

**步骤 3 — 内核级清单**：列出 `FooKernelTy : public GenericKernelTy` 要 override 的（`initImpl`、`launchImpl`、`maxGroupSize`）。

**步骤 4 — 边界说明（本讲核心结论之一）**：用一段话说明 `FooDeviceTy`（框架层）与上层 `DeviceTy`（libomptarget 层）的职责划分——前者负责「跨后端公共编排（异步包装、JIT、RPC、环境变量、参数与共享内存准备）+ 单后端硬件细节」，后者负责「OpenMP 用户面语义（双编号、映射表、上层 OMPT、分配 Kind 选择）」；二者通过 `DeviceTy::RTL->`（C 边界 `int32_t` 函数）衔接。

**对照参考**：把你列出的清单与 `plugins-nextgen/cuda/src/rtl.cpp` 里 `CUDAPluginTy` / `CUDADeviceTy` / `CUDAKernelTy` 的 override 列表逐项比对，检验是否覆盖了 CUDA 实现的那些方法。完整的 host 插件走读见下一讲 u3-l3。

**预期结果**：一份「新增 nextgen 插件的最小 override 骨架」文档，可作为日后真正动手写插件（或评审一个新插件）的检查清单。

**运行结果**：待本地验证（设计型实践，无需运行；若想验证清单完备性，可在本机构建 host 插件并阅读其实现）。

## 6. 本讲小结

- nextgen 插件框架的统一设计是**模板方法模式**：每个能力都是「非虚外壳（公共逻辑）+ 纯虚 `Impl`（硬件差异）」，公共逻辑集中在基类，后端只填空。
- 一次调用纵向穿越**三层方法**：上层 `DeviceTy`（用户面）→ `GenericPluginTy` 的 `int32_t` C 边界函数（UserId→RTLDeviceID 转换 + 错误降级）→ `GenericDeviceTy` 的 `Error` 外壳（公共编排）→ 后端纯虚 `Impl`（碰硬件）。
- `GenericPluginTy` 代表「一个后端」，负责探测硬件、预留设备槽、创建公共设施（GlobalHandler/JIT/RPC），暴露 `initImpl`/`createDevice`/`getMagicElfBits`/`getTripleArch` 等插件级纯虚契约。
- `GenericDeviceTy` 代表「一张卡」，是框架最庞大的基类；它的外壳（`init`/`loadBinary`/`launchKernel` 等）承担了 OMPT 回调、环境变量应用、JIT、RPC、参数与共享内存准备等大量公共工作，后端只需 override `...Impl`。
- 异步统一由 `AsyncInfoWrapperTy` 处理：外部 `AsyncInfo` 为空时用内部 `LocalAsyncInfo` 并在 `finalize` 时同步，使「同步」成为「异步 + 强制等待」的特例；队列/事件等可复用资源由 `GenericDeviceResourceManagerTy` 池化。
- 错误处理以 `llvm::Error` / `Expected` 贯穿框架内部（`OffloadError` + `ErrorCode` 枚举），只在最外层 C 边界降级成 `OFFLOAD_SUCCESS/FAIL` 并 `REPORT` 文案；设备信息统一建模为 `InfoTreeNode` 树，同时服务 `printInfo` 与 liboffload 查询。

## 7. 下一步学习建议

- **u3-l2 GenericKernelTy 与 DeviceImageTy**：本讲只点到 `GenericKernelTy::launch` 的编排，下一讲会展开内核对象的生命周期（`init`/`launch`/`maxGroupSize`）、`KernelLaunchEnvironment` 以及 `DeviceImageTy` 如何承载设备镜像与可选 IR image。
- **u3-l3 host 插件完整走读**：把本讲得到的「override 清单」对照最简单的 host 插件（`plugins-nextgen/host/src/rtl.cpp`）逐行印证，建立「最小可运行后端」的直觉。
- **横向阅读**：在进入 u3-l3 之前，建议先打开 `plugins-nextgen/cuda/src/rtl.cpp` 的 `CUDAKernelTy`（约 97 行）、`CUDADeviceTy`（约 273 行）、`CUDAPluginTy`（约 1665 行）三段，把本讲的基类与一个真实子类并排读一遍，体会「填空」的具体形态。
- **进阶**：完成上述走读后，可继续阅读 u3-l5（GlobalHandler）、u3-l6（MemoryManager）、u3-l7（RPC）、u3-l8（Record/Replay）、u3-l9（JIT），它们都是挂在本讲这两个基类上的横切机制。
