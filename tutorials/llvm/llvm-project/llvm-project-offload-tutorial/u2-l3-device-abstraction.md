# DeviceTy 设备抽象

## 1. 本讲目标

本讲聚焦 libomptarget 里最核心的一个数据结构：`DeviceTy`。读完本讲，你应当能够：

- 说清 `DeviceTy` 在整个运行时分层里扮演的角色——它是「上层 OpenMP 逻辑」与「底层设备插件」之间的桥（外观/转发层）。
- 掌握 `DeviceTy` 暴露给上层的接口：内存分配/释放（`allocData`/`deleteData`）、数据搬运（`submitData`/`retrieveData`/`dataExchange`）、内核启动（`launchKernel`）、事件与同步（`synchronize`/`queryAsync`/`createEvent` 等）。
- 理解 `DeviceTy` 如何被 `PluginManager` 构造、如何延迟加载镜像（`loadBinary`）、如何与 `MappingInfoTy`（主机-设备映射）协作。

本讲是 [u2-l2](u2-l2-plugin-manager.md) 的直接延续：上一讲讲了 `PluginManager` 如何把 OpenMP 设备号映射到「插件 + 插件内设备号」，本讲就钻进这个映射所指代的对象——`DeviceTy` 本身。

## 2. 前置知识

### 2.1 回顾：两种设备号

在 [u2-l2](u2-l2-plugin-manager.md) 中我们建立了一个关键结论：

- **`UserId`**：用户/OpenMP 面看到的设备号（从 0 连续编号），也就是 `omp_get_device_num()` 那一族 API 口径里的「设备号」。
- **`RTLDeviceID`**：某个插件（如 CUDA 插件）**内部**的设备下标，范围是该插件支持的设备数。

`PluginManager` 用一张正向表 `DeviceIds` 维护 `(插件指针, 插件内设备号) → UserId` 的映射。`DeviceTy` 这个对象，就是这两套编号在「对象层面」的具象化——它同时持有这两种编号。

### 2.2 什么是「转发层 / 外观（Facade）」

`DeviceTy` 自己**不直接操作任何硬件**。它不调用 CUDA Driver API，也不调用 Level Zero。它做的事情几乎都是：

> 收到上层请求 → 做一些前置工作（打印信息、触发 OMPT 工具回调）→ 把请求转发给底层插件 → 返回结果。

这种设计模式叫**外观模式（Facade）**。好处是：上层代码（如 `omptarget.cpp` 里的 `targetDataBegin`）只面对一个统一的 `DeviceTy`，不需要关心当前到底是 GPU 还是 CPU；而真正与硬件打交道的细节，全部封装在插件里（[u3-l1](u3-l1-plugin-interface.md) 会深入）。

### 2.3 异步上下文 AsyncInfoTy（极简版）

`DeviceTy` 的很多方法都带一个 `AsyncInfoTy &AsyncInfo` 参数。它代表一次「可能异步」的操作上下文（一个提交队列）。本讲只需把它理解成：

- 一个伴随每次数据搬运/内核启动的对象；
- 操作可能「挂」在它上面，稍后由 `synchronize()` 或 `queryAsync()` 来确认完成；
- 它的深入原理在 [u2-l7](u2-l7-async-model.md) 单独讲解。

### 2.4 成功/失败约定

插件接口沿用 C 风格的整数返回值，定义在 [include/omptarget.h:31-32](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L31-L32)：

- `OFFLOAD_SUCCESS` 等于 `0`；
- `OFFLOAD_FAIL` 等于 `~0`（即全 1）。

`DeviceTy` 里凡是返回 `int32_t` 的方法，都遵循这一约定。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/device.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h) | `DeviceTy` 结构体的声明，列出全部对外方法与私有成员。 |
| [libomptarget/device.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp) | `DeviceTy` 各方法的实现（也是本讲主力精读对象）。 |
| [plugins-nextgen/common/include/PluginInterface.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h) | `GenericPluginTy` 的公开接口——`DeviceTy` 转发的「目的地」。 |
| [include/omptarget.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h) | `AsyncInfoTy`、`TargetAllocTy`、`OFFLOAD_SUCCESS/FAIL` 等共享定义。 |
| [libomptarget/PluginManager.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp) | `initializeDevice` 构造 `DeviceTy`；`getDevice` 触发延迟加载。 |

## 4. 核心概念与源码讲解

### 4.1 DeviceTy 的定位：上层与插件之间的桥

#### 4.1.1 概念说明

`DeviceTy` 是一个普通的 C++ `struct`，**不可拷贝**（拷贝构造与赋值都被 `delete`）。它不持有任何硬件句柄，只持有三个关键字段，外加两个辅助成员：

| 成员 | 类型 | 含义 |
|------|------|------|
| `DeviceID` | `int32_t` | OpenMP 面的设备号（即 `UserId`）。 |
| `RTL` | `GenericPluginTy *` | 指向负责本设备的插件对象。 |
| `RTLDeviceID` | `int32_t` | 该插件**内部**的设备下标。 |
| `MappingInfo` | `MappingInfoTy` | 主机-设备指针映射表（见 [u2-l4](u2-l4-data-mapping.md)）。 |
| `DeviceOffloadEntries` | `ProtectedObj<...>` | 受锁保护的「offload 条目」表，用于调试打印。 |

**为什么需要这三个字段同时存在？** 因为运行时随时要在两套编号之间互转：

- 上层（OpenMP 用户）只认 `DeviceID`；
- 真正干活时要调用 `RTL->xxx(RTLDeviceID, ...)`，插件只认它自己的内部下标。

`DeviceTy` 把这对映射「固化」在一个对象里，于是任何一次转发都能写成 `RTL->某方法(RTLDeviceID, 其余参数)`。

#### 4.1.2 核心流程：从构造到使用

一个 `DeviceTy` 的生命周期大致是：

1. **构造**：`PluginManager::initializeDevice` 在初始化某插件设备时 `new` 出它（见下文源码）。
2. **`init()`**：调用 `RTL->init_device(RTLDeviceID)`，让插件把这块设备真正初始化好；并按需开启内核录制（Record/Replay）。
3. **延迟加载镜像**：首次被 `getDevice()` 取出时，若 `HasPendingImages` 为真，触发 `loadImagesOntoDevice`，进而调用 `Device.loadBinary(...)`。
4. **正常使用**：上层反复调用 `allocData`/`submitData`/`launchKernel`/`synchronize` 等。
5. **析构**：进程退出时随 `PluginManager` 一起销毁。

其中第 2、3 步体现了「构造 ≠ 初始化 ≠ 加载镜像」三级按需推进，这与 [u2-l1](u2-l1-runtime-entry.md)、[u2-l2](u2-l2-plugin-manager.md) 强调的「注册期登记、使用期加载」一脉相承。

#### 4.1.3 源码精读

**结构体与三个字段** —— [include/device.h:47-57](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h#L47-L57) 定义了 `DeviceTy` 与它的核心字段，并显式删除拷贝操作。注意第 49 行 `GenericPluginTy *RTL;` 就是「桥」的另一端。

**构造函数** —— [libomptarget/device.cpp:70-72](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L70-L72) 把三个编号写进字段，并把 `MappingInfo` 与本设备绑定：

```cpp
DeviceTy::DeviceTy(GenericPluginTy *RTL, int32_t DeviceID, int32_t RTLDeviceID)
    : DeviceID(DeviceID), RTL(RTL), RTLDeviceID(RTLDeviceID),
      MappingInfo(*this) {}
```

**谁在 new 它？** —— [libomptarget/PluginManager.cpp:107-118](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L107-L118)。关键在于实参顺序：`DeviceTy(&Plugin, UserId, DeviceId)`，对应「`RTL=&Plugin`、`DeviceID=UserId`、`RTLDeviceID=DeviceId`」：

```cpp
int32_t UserId = ExclusiveDevicesAccessor->size();      // OpenMP 设备号
auto Device = std::make_unique<DeviceTy>(&Plugin, UserId, DeviceId);
if (auto Err = Device->init()) { ... }
ExclusiveDevicesAccessor->push_back(std::move(Device));
PM->DeviceIds[std::make_pair(&Plugin, DeviceId)] = UserId;  // 反向映射
```

可见 `UserId` 就是「当前设备数组长度」，公式上即：设第 k 个插件有 nₖ 个设备，则该插件内第 j 号设备的 `UserId` 为

\[
\text{UserId}(P_k, j) = \sum_{i<k} n_i + j
\]

这与 [u2-l2](u2-l2-plugin-manager.md) 给出的编号规则完全一致。

**`init()`** —— [libomptarget/device.cpp:82-119](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L82-L119)。第 83 行调用 `RTL->init_device(RTLDeviceID)` 完成插件侧初始化；失败则用 `llvm::Error` 包装一个 `BACKEND_FAILURE` 错误返回（这是 nextgen 框架统一的错误处理风格，详见 [u3-l1](u3-l1-plugin-interface.md)）。随后读取若干 `LIBOMPTARGET_RECORD*` 环境变量，按需调用 `RTL->initialize_record_replay(...)` 开启内核录制。

#### 4.1.4 代码实践：画出延迟加载链

1. **实践目标**：理解 `DeviceTy` 是何时、由谁触发镜像加载的。
2. **操作步骤**：
   - 打开 [libomptarget/PluginManager.cpp:553-573](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L553-L573) 的 `PluginManager::getDevice`。
   - 注意第 567-568 行：只要 `DevicePtr->hasPendingImages()` 为真，就调用 `loadImagesOntoDevice(*DevicePtr)`。
   - 再打开 [libomptarget/PluginManager.cpp:390-551](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L390-L551)，定位第 425 行 `Device.loadBinary(Img)`。
3. **需要观察的现象**：`DeviceTy` 自身**没有**在构造/`init` 时加载镜像；真正加载发生在「上层第一次通过 `getDevice(DeviceNo)` 取用它」时。
4. **预期结果**：你能画出一条调用链：`getDevice(n)` →（若 `HasPendingImages`）`loadImagesOntoDevice` → `Device.loadBinary` → `RTL->load_binary`。
5. 待本地验证：可在 `getDevice` 与 `loadBinary` 各加一行 `fprintf(stderr, ...)` 日志，运行任意 target 程序确认触发顺序。

#### 4.1.5 小练习与答案

**练习 1**：`DeviceTy` 为什么同时保存 `DeviceID` 和 `RTLDeviceID`？只留一个不行吗？

> **参考答案**：因为这两套编号服务的对象不同。`DeviceID`（UserId）是 OpenMP 用户面的连续编号，用于上报和查找；`RTLDeviceID` 是插件内部下标，调用 `RTL->xxx(RTLDeviceID,...)` 时必须用插件自己的口径。两者无法互推（除非借助 `PluginManager` 的表），所以都存下来最直接。

**练习 2**：`DeviceTy` 的拷贝构造为什么被 `delete`？

> **参考答案**：`DeviceTy` 持有 `MappingInfoTy`（内部有映射表与锁）以及与插件、设备号的强绑定关系，属于「身份型」对象而非「值型」对象。整个运行时里每个设备只有唯一一个 `DeviceTy` 实例，靠指针（`DeviceIds` 表、设备数组）引用，禁止拷贝可避免出现两个对象指向同一底层设备却各持一份映射表的混乱。

---

### 4.2 数据搬运：submit / retrieve / exchange

#### 4.2.1 概念说明

OpenMP 卸载有三类主机-设备间的数据移动：

| 方向 | 方法 | 对应插件调用 |
|------|------|--------------|
| 主机 → 设备（H2D） | `submitData` | `RTL->data_submit_async` |
| 设备 → 主机（D2H） | `retrieveData` | `RTL->data_retrieve_async` |
| 设备 → 设备（D2D） | `dataExchange` | `RTL->data_exchange[_async]` |

这三个方法都接受一个 `AsyncInfoTy &AsyncInfo`，意味着它们默认是**异步**的——数据搬运被提交到设备的队列里，函数立即返回，真正的完成要靠后续的 `synchronize()` 或 `queryAsync()` 来确认。

另外，每个方法在被编译进带 OMPT 支持的运行时（`OMPT_SUPPORT`）时，都会用一种 RAII 手段在搬运前后派发工具回调，使外接的 OMPT 工具能观测到每一次数据移动。

#### 4.2.2 核心流程：submitData 的一次调用

以「主机 → 设备」为例，`submitData(TgtPtrBegin, HstPtrBegin, Size, AsyncInfo, ...)` 做三件事：

1. 若用户开了信息打印（`OMP_INFOTYPE_DATA_TRANSFER` 位），调用 `MappingInfo.printCopyInfo(...)` 打印本次搬运的源、目的、大小。
2. （仅 OMPT 构建时）构造一个 `InterfaceRAII` 对象，进入时触发 `ompt_target_data_transfer_to_device` 的「before」回调，离开时触发「after」回调。
3. 调用 `RTL->data_submit_async(RTLDeviceID, TgtPtrBegin, HstPtrBegin, Size, AsyncInfo)`，把工作真正交给插件，并返回插件的 `OFFLOAD_SUCCESS/FAIL`。

`retrieveData` 几乎是镜像版本，区别只在回调枚举（`ompt_target_data_transfer_from_device`）和参数顺序。

`dataExchange` 多一个分支：当 `AsyncInfo` 无效（`if (!AsyncInfo)`）时退化为同步的 `RTL->data_exchange`，否则走异步的 `RTL->data_exchange_async`。

#### 4.2.3 源码精读

**submitData** —— [libomptarget/device.cpp:272-288](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L272-L288)。注意三段式结构（打印 → OMPT RAII → 转发），最后一行才是真正干活：

```cpp
return RTL->data_submit_async(RTLDeviceID, TgtPtrBegin, HstPtrBegin, Size,
                              AsyncInfo);
```

**retrieveData** —— [libomptarget/device.cpp:291-308](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L291-L308)，结构与 `submitData` 对称，转发到 `RTL->data_retrieve_async`。

**dataExchange** —— [libomptarget/device.cpp:311-330](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L311-L330)。注意第 324 行的同步/异步分支判断。

**「原子搬运」的事件保护** —— [libomptarget/device.cpp:43-68](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L43-L68) 的 `HostDataToTargetTy::addEventIfNecessary`。当 `MappingConfig::get().UseEventsForAtomicTransfers` 为真时，运行时会给一次「原子」式的搬运关联一个事件（`createEvent` → `recordEvent`），让后续依赖它的操作能正确排队。这里直接用到了 `DeviceTy` 的事件接口，体现了 4.5 节事件 API 与数据搬运的耦合。

#### 4.2.4 代码实践：submitData 对照 GenericPluginTy

1. **实践目标**：亲眼确认 `DeviceTy::submitData` 转发到的到底是 `GenericPluginTy` 的哪个方法、参数如何对应。
2. **操作步骤**：
   - 在 [libomptarget/device.cpp:272-288](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L272-L288) 读到转发目标是 `RTL->data_submit_async(RTLDeviceID, TgtPtrBegin, HstPtrBegin, Size, AsyncInfo)`。
   - 打开 [plugins-nextgen/common/include/PluginInterface.h:1692-1693](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1692-L1693)，对照 `GenericPluginTy::data_submit_async` 的签名。
3. **需要观察的现象**：参数个数、顺序、类型应当一一对应；`AsyncInfo` 经由 `AsyncInfoTy::operator __tgt_async_info *()`（[include/omptarget.h:146](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L146)）隐式转成 C 结构 `__tgt_async_info *` 传入插件。
4. **预期结果**：你能填出这张对照表——

   | `DeviceTy::submitData` 形参 | 传给 `data_submit_async` 的实参 |
   |---|---|
   | `TgtPtrBegin` | `TgtPtr` |
   | `HstPtrBegin` | `HstPtr` |
   | `Size` | `Size` |
   | `AsyncInfo` | 隐式转为 `__tgt_async_info *` |

#### 4.2.5 小练习与答案

**练习 1**：为什么 `submitData`/`retrieveData` 总是异步的，而 `dataExchange` 却有同步分支？

> **参考答案**：H2D/D2H 几乎总在 OpenMP target 区域的 `AsyncInfo` 上下文里发生，天然异步；而 D2D 交换在某些路径（例如镜像加载期解析 indirect 表，见 4.5 节）可能没有可用异步句柄，`dataExchange` 用 `if (!AsyncInfo)` 判断，无句柄时退化为同步 `data_exchange`，保证无 `AsyncInfo` 也能工作。

**练习 2**：`submitData` 里的 `OMPT_IF_BUILT(...)` 块如果没开启 OMPT 支持，会发生什么？

> **参考答案**：`OMPT_IF_BUILT` 是条件编译宏，未开启 OMPT 支持时这段 RAII 代码根本不存在，函数体实质只剩「按需打印 + 一次转发」，零额外开销。这就是运行时用 RAII 派发回调却能保持非 OMPT 构建高性能的原因。

---

### 4.3 内存分配与释放：allocData / deleteData

#### 4.3.1 概念说明

设备上的内存并不只有一种。OpenMP 定义了几种「分配器空间」（[include/omptarget.h:105-111](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L105-L111)）：

| 枚举 | 含义 |
|------|------|
| `TARGET_ALLOC_DEVICE` | 设备私有显存。 |
| `TARGET_ALLOC_HOST` | 主机端可被设备访问的内存（pinned/managed）。 |
| `TARGET_ALLOC_SHARED` | 主机与设备共享的统一内存。 |
| `TARGET_ALLOC_DEFAULT` | 由运行时/插件决定（默认值）。 |

`DeviceTy::allocData(Size, HstPtr, Kind)` 把这三个参数交给插件，由插件依据 `Kind` 选择对应分配器。注意 `HstPtr`（主机地址）**仅作提示**，注释里明确说所有实现都忽略它、不做指针关联——真正建立主机↔设备指针映射是 `MappingInfoTy` 的职责（[u2-l4](u2-l4-data-mapping.md)）。

#### 4.3.2 核心流程

- `allocData`：可选地用 RAII 派发 `ompt_target_data_alloc` 回调 → 调用 `RTL->data_alloc(RTLDeviceID, Size, HstPtr, Kind)` → 返回设备指针（失败为 `nullptr`）。
- `deleteData`：用 RAII 派发 `ompt_target_data_delete` 回调 → 调用 `RTL->data_delete(RTLDeviceID, TgtPtr, Kind)` → 返回 `OFFLOAD_SUCCESS/FAIL`。

#### 4.3.3 源码精读

**allocData** —— [libomptarget/device.cpp:249-259](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L249-L259)。注意第 257 行才是真正分配：`TargetPtr = RTL->data_alloc(RTLDeviceID, Size, HstPtr, Kind);`

**deleteData** —— [libomptarget/device.cpp:261-269](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L261-L269)，转发到 `RTL->data_delete`。

对照 [plugins-nextgen/common/include/PluginInterface.h:1669](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1669) 的 `void *data_alloc(int32_t DeviceId, int64_t Size, void *HostPtr, int32_t Kind);` 与第 1672 行 `data_delete`，可见签名完全对齐。

> 提示：插件内部还套了一层 free-list 内存管理器（OOM 时释放空闲链表再重试），那是 [u3-l6](u3-l6-memory-manager.md) 的主题，本讲只需知道 `data_alloc`/`data_delete` 最终落到那里即可。

#### 4.3.4 代码实践：追踪一次 allocData 的来源

1. **实践目标**：看清上层是谁、在何时调用 `DeviceTy::allocData`。
2. **操作步骤**：
   - 用 `Grep` 在 `libomptarget/` 下搜索 `.allocData(` 的调用点（典型出现在 `omptarget.cpp` 的 `targetDataBegin` 路径与 `OpenMP/API.cpp` 的 `omp_target_alloc` 路径）。
   - 选一个调用点，记录它传入的 `Kind` 值。
3. **需要观察的现象**：不同上层 API 会传不同的 `Kind`——例如 `omp_target_alloc` 默认 `TARGET_ALLOC_DEVICE`，而 `map(alloc:)` 走 `TARGET_ALLOC_DEFAULT`。
4. **预期结果**：你能说出「上层语义 → `Kind` 枚举 → `data_alloc` 实参」这条链路。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习**：`allocData` 的 `HstPtr` 参数既然被所有插件忽略，为什么还保留在接口里？

> **参考答案**：它是为「未来可能的提示性优化」预留的——某些后端（如支持统一内存或按主机地址做镜像分配的设备）理论上可以利用主机地址做出更优的分配决策；同时它也便于 OMPT 回调向上层报告「这次分配与哪段主机数据相关」。当前实现选择忽略，但接口先留着，避免日后破坏 ABI。

---

### 4.4 内核启动：launchKernel

#### 4.4.1 概念说明

当数据就位后，`__tgt_target_kernel`（详见 [u2-l6](u2-l6-kernel-launch-flow.md)）最终会调到 `DeviceTy::launchKernel`，把一段已经加载到设备上的内核真正跑起来。它的参数对应一次内核启动的全部要素：

| 参数 | 含义 |
|------|------|
| `TgtEntryPtr` | 设备端内核入口地址（由 `loadBinary` 解析得到）。 |
| `TgtVarsPtr` | 指向「参数指针数组」的指针（每个参数一个 `void*`）。 |
| `TgtOffsets` | 各参数的偏移量数组。 |
| `KernelArgs` | 内核启动参数（维度、组大小、循环次数等），类型 `KernelArgsTy`（[include/Shared/APITypes.h:91](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L91)）。 |
| `KernelExtraArgs` | 额外启动参数（`KernelExtraArgsTy`，[include/Shared/APITypes.h:150](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L150)），可空。 |
| `AsyncInfo` | 异步上下文。 |

#### 4.4.2 核心流程

`launchKernel` 是本讲里**最薄的转发**之一：它不做打印、不做 OMPT 回调（内核派发的回调在上层 `__tgt_target_kernel` 处理），直接把全部参数原样转发给 `RTL->launch_kernel`。

#### 4.4.3 源码精读

**launchKernel** —— [libomptarget/device.cpp:358-364](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L358-L364)：

```cpp
int32_t DeviceTy::launchKernel(void *TgtEntryPtr, void **TgtVarsPtr,
                               ptrdiff_t *TgtOffsets, KernelArgsTy &KernelArgs,
                               KernelExtraArgsTy *KernelExtraArgs,
                               AsyncInfoTy &AsyncInfo) {
  return RTL->launch_kernel(RTLDeviceID, TgtEntryPtr, TgtVarsPtr, TgtOffsets,
                            &KernelArgs, KernelExtraArgs, AsyncInfo);
}
```

对照插件侧 [plugins-nextgen/common/include/PluginInterface.h:1717-1720](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1717-L1720) 的 `GenericPluginTy::launch_kernel` 签名——除多了 `RTLDeviceID` 这一首参数外，其余完全一致，是一次「近乎透明」的转发。

#### 4.4.4 代码实践：launchKernel 对照 GenericPluginTy

1. **实践目标**：完成「实践任务」的另一半——把 `launchKernel` 与 `submitData` 放在一起，对比它们各自调到了 `GenericPluginTy` 的哪个底层接口。
2. **操作步骤**：
   - 把 [device.cpp:358-364](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L358-L364) 与 [device.cpp:272-288](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L272-L288) 并排阅读。
   - 在 [PluginInterface.h:1692-1693](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1692-L1693) 与 [PluginInterface.h:1717-1720](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1717-L1720) 找到两个目标方法。
3. **需要观察的现象**：`submitData` 在转发前有「打印 + OMPT RAII」两段预处理；`launchKernel` 几乎是「裸转发」。
4. **预期结果**：你能填出下表——

   | `DeviceTy` 方法 | 预处理 | 转发到的 `GenericPluginTy` 方法 |
   |---|---|---|
   | `submitData` | `printCopyInfo` + `ompt_target_data_transfer_to_device` RAII | `data_submit_async` |
   | `launchKernel` | 无 | `launch_kernel` |

5. **思考延伸**：为什么 `launchKernel` 不在这里做 OMPT 回调？因为内核派发的 before/after 回调由上层 `__tgt_target_kernel` 统一派发（见 [u2-l6](u2-l6-kernel-launch-flow.md) 与 [u3-l10](u3-l10-ompt-tooling.md)），避免重复。

#### 4.4.5 小练习与答案

**练习**：`launchKernel` 为什么把 `KernelArgs` 按**引用**接收、却又按**指针** `&KernelArgs` 转发给插件？

> **参考答案**：上层调用更自然用引用（保证非空、语义清晰）；而插件 C 风格接口 `launch_kernel(..., KernelArgsTy *KernelArgs, ...)` 用指针，是为了与「可能为空」的 `KernelExtraArgsTy *` 保持一致的指针风格，也方便插件内部把「参数块」当作一块连续内存去读取或录制。`&KernelArgs` 就是引用→指针的桥接。

---

### 4.5 事件、同步、查询与 offload 条目

#### 4.5.1 概念说明

**同步 vs 查询**。异步操作提交后，有两种确认完成的方式：

- `synchronize(AsyncInfo)`：**阻塞**当前线程，直到队列里所有挂起操作完成。
- `queryAsync(AsyncInfo)`：**非阻塞**地探询一次；可能返回「未完成」，需被多次调用，直到 `AsyncInfo.isDone()` 为真。

后者是 `target nowait` 区域得以非阻塞同步的基础（详见 [u2-l7](u2-l7-async-model.md)）。

**事件（Event）**。事件是设备队列里的「书签」，用于在多次操作之间建立依赖。`DeviceTy` 暴露五个事件原语：`createEvent` / `recordEvent` / `waitEvent` / `syncEvent` / `destroyEvent`。它们都被 4.2 节的 `addEventIfNecessary` 用来保护原子搬运。

**offload 条目与符号解析**。设备镜像里有两类符号需要解析：

- **内核函数**：`Entry.Size == 0` 的条目，用 `RTL->get_function(Binary, Name, &Ptr)` 取到设备端入口地址。
- **全局变量**：`Entry.Size != 0` 的条目，用 `RTL->get_global(Binary, Size, Name, &Ptr)` 取到设备端变量地址。

这件事在两个地方发生：一是 `DeviceTy::loadBinary`（见下），二是 `PluginManager::loadImagesOntoDevice` 在构建翻译表时逐条解析（[libomptarget/PluginManager.cpp:435-471](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L435-L471)）。

#### 4.5.2 核心流程：loadBinary 做了什么

`DeviceTy::loadBinary(Img)` 的流程：

1. 调 `RTL->load_binary(RTLDeviceID, Img, &Binary)`，让插件把镜像加载到设备，返回一个 `__tgt_device_binary` 句柄。
2. 尝试 `RTL->get_global(Binary, ..., "__omp_rtl_device_environment", ...)`，找（可选的）设备环境符号；找不到就直接返回 `Binary`。
3. 调 `setupIndirectCallTable(...)` 处理 OpenMP `indirect` 间接调用条目，构造一张「主机函数指针 → 设备函数指针」的表，并上传到设备。
4. 组装 `DeviceEnvironmentTy`（调试级别、设备数、时钟频率、硬件并行度、间接调用表地址等），用 `submitData` 把它写到设备上的那个环境符号里。
5. 返回 `Binary`。

#### 4.5.3 源码精读

**synchronize / queryAsync** —— [libomptarget/device.cpp:382-388](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L382-L388)，分别转发到 `RTL->synchronize` 与 `RTL->query_async`。

**事件五件套** —— [libomptarget/device.cpp:390-408](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L390-L408)，每一件都是一行转发（`create_event` / `record_event` / `wait_event` / `sync_event` / `destroy_event`）。

**loadBinary** —— [libomptarget/device.cpp:207-247](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L207-L247)。注意第 226 行通过 `RTL->getDevice(RTLDeviceID)` 拿到底层 `GenericDeviceTy`，从中读取 `getDebugKind()`、`getClockFrequency()`、`getHardwareParallelism()` 填充设备环境——这是 `DeviceTy` 少数几处「越过 `GenericPluginTy` 公共转发层、直接接触 `GenericDeviceTy`」的地方。

**setupIndirectCallTable** —— [libomptarget/device.cpp:125-204](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L125-L204)：遍历镜像条目，对带 `OMP_DECLARE_TARGET_INDIRECT` / `OMP_DECLARE_TARGET_INDIRECT_VTABLE` 标志的条目，用 `RTL->get_global` 取设备地址、用 `retrieveData` 把设备端函数指针读回主机，组装成表后用 `allocData`+`submitData` 上传。

**dumpOffloadEntries** —— [libomptarget/device.cpp:410-420](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L410-L420)，遍历私有成员 `DeviceOffloadEntries`（[include/device.h:192-194](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h#L192-L194)）按 kernel / link / global var 分类打印，是 `OMPTARGET_DUMP_OFFLOAD_ENTRIES` 环境变量触发的调试入口（见 [PluginManager.cpp:545-548](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L545-L548)）。

> 说明：`DeviceOffloadEntries` 这个 `ProtectedObj<DenseMap>` 是 `DeviceTy` 自带的「条目缓存容器」，但当前 HEAD 下条目的**权威翻译结果**实际存放在 `PluginManager` 的 `TranslationTable`（`TargetsEntries` / `TargetsTable`，见 [include/rtl.h:27-42](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/rtl.h#L27-L42)）。也就是说，`DeviceTy` 负责「加载镜像 + 解析符号 + 把条目交给 PluginManager 建表」，而 `DeviceOffloadEntries` 目前仅服务于调试打印。理解这一分工，能避免在学习时把两处条目表混淆。

#### 4.5.4 代码实践：读懂一次 synchronize 的去向

1. **实践目标**：确认 `synchronize` 与 `queryAsync` 转发到插件后，分别对应 `GenericPluginTy` 的哪个方法。
2. **操作步骤**：
   - 阅读 [device.cpp:382-388](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L382-L388)。
   - 在 [PluginInterface.h:1723](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1723) 与 [PluginInterface.h:1726](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1726) 找到 `synchronize` 与 `query_async`。
3. **需要观察的现象**：两者签名一致（都吃 `DeviceId` + `AsyncInfoPtr`），区别只在语义——阻塞 vs 非阻塞探询。
4. **预期结果**：你能解释为什么 `AsyncInfoTy` 的析构函数（[include/omptarget.h:142](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L142) `~AsyncInfoTy() { synchronize(); }`）能保证「离开作用域即同步完成」——因为 `synchronize()` 最终会走到设备的阻塞同步。
5. 待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`synchronize` 和 `queryAsync` 都返回 `int32_t`，怎么判断「完成」？

> **参考答案**：返回值只表示「这次调用本身有没有出错」（`OFFLOAD_SUCCESS/FAIL`），**不**表示操作是否完成。是否完成要看 `AsyncInfo.isDone()`（[include/omptarget.h:170](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L170)）。`queryAsync` 必须被反复调用，直到 `isDone()` 为真。

**练习 2**：`loadBinary` 里第 226 行 `RTL->getDevice(RTLDeviceID)` 返回的 `GenericDeviceTy` 和 `DeviceTy` 是什么关系？

> **参考答案**：它们是两个层次的「设备」对象。`DeviceTy` 是 libomptarget 上层的设备抽象（本讲主角），`GenericDeviceTy` 是 nextgen 插件框架内部的设备基类（[u3-l1](u3-l1-plugin-interface.md)）。`DeviceTy` 通过 `RTL->getDevice(RTLDeviceID)` 「向下」取到对应的 `GenericDeviceTy`，读取一些只有插件层才知道的硬件属性（时钟频率、硬件并行度等）。

---

## 5. 综合实践

把本讲的知识串起来，跟踪一次完整的「主机→设备数据搬运 + 内核启动」：

1. 写一个最小的 OpenMP target 程序（含 `map(to:)` 与 `target parallel for`）。
2. 编译并运行（工具链用法见 [u1-l4](u1-l4-toolchain-and-run.md)），开启 `LIBOMPTARGET_INFO=63`。
3. 对照源码，在以下五个位置各加一行 `fprintf(stderr, "HERE: %s\n", __func__);`（**仅为阅读型实践，建议在本地副本上修改，勿提交**）：
   - `PluginManager::getDevice`（[PluginManager.cpp:553](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L553)）
   - `DeviceTy::loadBinary`（[device.cpp:208](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L208)）
   - `DeviceTy::submitData`（[device.cpp:272](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L272)）
   - `DeviceTy::launchKernel`（[device.cpp:358](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L358)）
   - `DeviceTy::synchronize`（[device.cpp:382](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L382)）
4. **预期观察到的顺序**：`getDevice`（首次触发）→ `loadBinary` →（运行期）`submitData` → `launchKernel` → `synchronize`。
5. 把每一步与它最终转发到的 `GenericPluginTy` 方法写成一页调用链笔记，作为后续学习 [u2-l5](u2-l5-target-data-flow.md)、[u2-l6](u2-l6-kernel-launch-flow.md) 的脚手架。

## 6. 本讲小结

- `DeviceTy` 是 libomptarget 与设备插件之间的**外观/转发层**：自己不碰硬件，把上层请求翻译成 `RTL->xxx(RTLDeviceID, ...)`。
- 它同时持有 `DeviceID`（OpenMP 用户号）与 `RTLDeviceID`（插件内下标），是 [u2-l2](u2-l2-plugin-manager.md) 双向编号映射的对象化。
- 内存接口 `allocData`/`deleteData` 按 `Kind` 选择设备/主机/共享空间，转发到 `data_alloc`/`data_delete`。
- 数据搬运 `submitData`/`retrieveData`/`dataExchange` 默认异步，转发前做「信息打印 + OMPT 回调」预处理。
- `launchKernel` 是近乎透明的转发，把内核启动的全部要素交给 `GenericPluginTy::launch_kernel`。
- `loadBinary` 负责加载镜像、解析 indirect 调用表、上传设备环境；镜像的「按需加载」由 `PluginManager::getDevice` 在首次使用时触发。

## 7. 下一步学习建议

- **[u2-l4 主机-设备数据映射机制](u2-l4-data-mapping.md)**：`DeviceTy` 内嵌的 `MappingInfoTy` 到底如何把主机指针映射成设备指针、如何用引用计数决定数据何时搬运与释放——这是本讲的直接后续。
- **[u2-l5 target data 流程](u2-l5-target-data-flow.md) / [u2-l6 内核启动流程](u2-l6-kernel-launch-flow.md)**：站在 `DeviceTy` 之上往回看，理解 `targetDataBegin`/`__tgt_target_kernel` 是如何编排调用本讲这些接口的。
- **[u2-l7 异步模型](u2-l7-async-model.md)**：深入 `AsyncInfoTy` / `TaskAsyncInfoWrapperTy`，理解 `nowait` 区域的非阻塞同步。
- **[u3-l1 通用插件接口](u3-l1-plugin-interface.md)**：向下钻进 `GenericPluginTy` / `GenericDeviceTy`，看清本讲所有 `RTL->xxx` 调用的真正实现与虚函数契约。
