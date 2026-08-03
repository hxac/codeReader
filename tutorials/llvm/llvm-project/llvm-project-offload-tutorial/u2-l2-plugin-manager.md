# PluginManager 设备与插件管理

## 1. 本讲目标

学完本讲后，你应当能够：

- 说明 `PluginManager` 在 libomptarget 中的角色，以及它持有哪些核心数据（插件、设备、翻译表、需求集合）。
- 解释多插件环境下「OpenMP 设备号」是如何分配的，并能写出「设备号 ↔（插件，插件内设备号）」的双向映射伪代码。
- 理解设备容器为什么用 `ProtectedObj` / `Accessor` 做独占访问，运行时一共有哪几把互斥锁各守什么。
- 理解 `RequirementCollection` 如何在注册期收集 `omp requires` 标志并跨翻译单元做一致性检查，以及 APU 上的 Auto Zero-Copy 是如何被自动加入的。
- 理解「延迟注册」解决的是什么时序竞态，以及「注册期登记、使用期加载」的按需设计。

## 2. 前置知识

阅读本讲前，建议你已经掌握 [u1-l3 目录结构与模块全景](u1-l3-directory-map.md) 里建立的分层地图，以及 [u2-l1 运行时初始化与库注册入口](u2-l1-runtime-entry.md) 里讲过的几个事实：

- 运行时是一个**被驱动的全局单例**：靠引用计数 `RefCount` 实现「首次进入构造、末次退出销毁」。
- `__tgt_register_lib` 会先把二进制描述符 `__tgt_bin_desc` **登记**进翻译表，但**真正的镜像加载（`loadBinary`）被推迟**到设备首次被使用时。
- 翻译表以 OpenMP 设备号 `UserId` 为下标，每个 `UserId` 对应「某个插件的某个设备」。

本讲就回答这个映射背后的管理者：`PluginManager`。它本身不是插件，而是**插件的容器与调度者**——把异构的后端（host / cuda / amdgpu / level_zero）统一成一组从 0 开始连续编号的 OpenMP 设备。

两个术语先约定清楚：

- **插件（plugin）**：一个 `GenericPluginTy` 子类实例，对应一种后端（如 CUDA）。一个插件可以管理多块同类型硬件。
- **设备号**：本讲里会出现两种「设备号」。OpenMP 用户面看到的、从 0 连续编号的整数叫 **UserId**（或 OpenMP 设备号）；而插件内部对该硬件的局部编号叫 **DeviceId**（或 `RTLDeviceID`）。本讲的核心就是把两者对上号。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`include/PluginManager.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h) | `PluginManager` 结构体声明：对外接口（`init`/`getDevice`/`registerLib`…）与私有数据成员（`Plugins`/`DeviceIds`/`Devices`/`Requirements`…）。 |
| [`libomptarget/PluginManager.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp) | 上述接口的实现，以及设备镜像按需加载的核心函数 `loadImagesOntoDevice`。 |
| [`include/ExclusiveAccess.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/ExclusiveAccess.h) | `ProtectedObj` / `Accessor`：把「对象 + 互斥锁」打包，靠 RAII 实现独占访问。 |
| [`include/Shared/Requirements.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h) | `RequirementCollection`：收集并校验 `omp requires` 标志。 |
| [`libomptarget/OffloadRTL.cpp`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OffloadRTL.cpp) | `initRuntime` / `deinitRuntime`：创建全局 `PM` 单例并驱动它的初始化与延迟注册重放。 |
| [`include/device.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h) | `DeviceTy` 的字段（`DeviceID` / `RTL` / `RTLDeviceID`），即「反向映射」的载体。 |

---

## 4. 核心概念与源码讲解

### 4.1 PluginManager 是什么：全局单例与核心数据成员

#### 4.1.1 概念说明

一台机器上可能同时装了多种加速器：两块 NVIDIA GPU、一块 AMD GPU，外加一个把主机 CPU 当设备的 host 插件。OpenMP 用户并不关心这些后端的差异，他只想说「在 0 号设备上跑这个 kernel」。`PluginManager` 就是中间的翻译层：

- 向下：持有一组异构**插件**对象，每个插件负责一种后端。
- 向上：把它们展平成一组从 0 开始连续编号的、同构的 `DeviceTy`，供 libomptarget 的上层（数据映射、kernel 启动、用户 API）统一使用。

它是一个**进程级全局单例**，由 `extern PluginManager *PM` 暴露：

[include/PluginManager.h:192-194](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L192-L194) —— 声明全局指针 `PM`，以及两个用于安全去初始化的原子量 `RTLAlive`、`RTLOngoingSyncs`（在 [u2-l1](u2-l1-runtime-entry.md) 已讲过它们的用途）。

#### 4.1.2 核心数据成员

`PluginManager` 的私有成员就是它的「账本」。下面这张表把账本逐项对应到源码：

| 成员 | 类型 | 作用 |
| --- | --- | --- |
| `Plugins` | `SmallVector<unique_ptr<GenericPluginTy>>` | 所有插件对象，按构造顺序排列（无论是否真正初始化）。 |
| `DeviceIds` | `DenseMap<pair<Plugin*,int32_t>, int32_t>` | **正向映射**：`(插件指针, 插件内设备号) → OpenMP 设备号 UserId`。 |
| `Devices` | `ProtectedObj<SmallVector<unique_ptr<DeviceTy>>>` | 以 `UserId` 为下标的设备数组，访问受互斥锁保护。 |
| `DeviceImages` | `SmallVector<unique_ptr<DeviceImageTy>>` | 从二进制描述符解析出的设备镜像。 |
| `UsedImages` | `DenseSet<const __tgt_device_image*>` | 已被某插件认领的镜像，用于去重。 |
| `Requirements` | `RequirementCollection` | 用户 `omp requires` 标志集合。 |
| `RTLsMtx` / `TrlTblMtx` / `TblMapMtx` | `std::mutex` | 分别保护注册流程、翻译表、主机指针→表项映射。 |
| `RTLsLoaded` / `DelayedBinDesc` | `bool` / `SmallVector<__tgt_bin_desc*>` | 延迟注册的暂存区。 |

源码位置见 [include/PluginManager.h:153-184](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L153-L184)。

其中最关键的是 `Plugins`、`DeviceIds`、`Devices` 三者构成的**双向映射**，这是下一节的主题。

> 小结：`PluginManager` 不是「又一个插件」，而是把异构后端「拍扁」成连续编号设备的调度中枢；它的一切机制都围绕「插件清单 / 设备编号 / 并发安全 / 注册时序」展开。

### 4.2 插件的构造与枚举

#### 4.2.1 概念说明：X-macro 模式与 `createPlugin_*`

插件列表是**构建期决定**的：CMake 根据你在 u1-l2 见过的 `LIBOMPTARGET_PLUGINS_TO_BUILD`，把每个插件展开成一行 `PLUGIN_TARGET(name)`，生成构建产物 `Shared/Targets.def`。模板是源码里的：

[include/Shared/Targets.def.in:14-20](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Targets.def.in#L14-L20) —— 模板要求使用方先 `#define PLUGIN_TARGET(Name)` 再 `#include` 它，中间那一行 `@LIBOMPTARGET_ENUM_PLUGIN_TARGETS@` 会被替换成 `PLUGIN_TARGET(host)`、`PLUGIN_TARGET(cuda)` 等。

生成逻辑见 [CMakeLists.txt:201-210](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/CMakeLists.txt#L201-L210)。这是经典的 **X-macro** 技巧：同一份列表被包含两次，每次用不同宏定义得到不同展开。

每个插件都必须导出一个工厂函数 `createPlugin_<name>()`，返回一个 `GenericPluginTy*`。例如 host 插件：

[plugins-nextgen/host/src/rtl.cpp:576-580](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/host/src/rtl.cpp#L576-L580) —— host 插件的工厂，`new` 出一个 `GenELF64PluginTy`。（CUDA/AMDGPU/Level Zero 同理，分别在 `cuda/src/rtl.cpp`、`amdgpu/src/rtl.cpp`、`level_zero/src/L0Plugin.cpp` 导出 `createPlugin_cuda`/`createPlugin_amdgpu`/`createPlugin_level_zero`。）

#### 4.2.2 核心流程：`init` 与 `deinit`

`init()` 用 X-macro 把每个插件的工厂结果塞进 `Plugins` 数组：

[libomptarget/PluginManager.cpp:30-51](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L30-L51) —— 第 30-31 行先 `#define` 出 `extern "C"` 声明，第 43-48 行再 `#define` 出「调用工厂并 emplace」的展开。注意此时**只构造对象、不初始化**——是否真的去探测硬件，要等到后面 `initializePlugin`。

要点：

1. 若 `OffloadPolicy::isOffloadDisabled()`（用户通过环境变量关闭了卸载），`init` 直接返回，一个插件都不加载。
2. `Plugins` 的顺序就是 `Targets.def` 里的展开顺序（即构建期插件清单的顺序），这个顺序**决定了后续设备编号的基底**。

`deinit()` 则反向清理，只对已初始化的插件调用 `Plugin->deinit()`：

[libomptarget/PluginManager.cpp:53-69](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L53-L69) —— 遍历 `Plugins`，跳过未初始化者，对失败用 `toString` 记录后 `release()`。

#### 4.2.3 源码精读：`initializePlugin` 与「活跃插件」

「构造」与「初始化」是分开的两步。`initializePlugin` 才真正调用插件的 `init()` 去探测硬件、打开 native 库：

[libomptarget/PluginManager.cpp:71-85](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L71-L85) —— 若已初始化则幂等返回；否则调用 `Plugin.init()`，成功后打印「Registered plugin X with N visible device(s)」。注意 `Plugin.init()` 返回 `llvm::Error`，失败时用 `toString` 提取信息后返回 `false`（基于 `llvm::Error` 的统一错误处理）。

> 这条日志正是观察设备编号的最佳入口（见 4.3 的实践）。

`getNumActivePlugins` 体现「活跃」语义——只数已初始化的插件：

[include/PluginManager.h:144-151](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L144-L151) —— 遍历 `plugins()`，对每个 `is_initialized()` 为真的计数。

而遍历接口 `plugins()` 返回的是「所有插件，无论是否在用」：

[include/PluginManager.h:132-135](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L132-L135) —— 用 `llvm::make_pointee_range` 把 `unique_ptr<Plugin>` 序列透明成 `Plugin&` 序列。

#### 4.2.4 代码实践：观察插件构造顺序

1. **实践目标**：确认 `Plugins` 的顺序由构建期 `Targets.def` 决定，且「构造」≠「初始化」。
2. **操作步骤**：阅读上面引用的 `init()` X-macro 片段；再阅读你本机构建目录下生成的 `include/Shared/Targets.def`（注意它是**构建产物**，源码里只有 `.def.in` 模板）。
3. **需要观察的现象**：生成的 `Targets.def` 里 `PLUGIN_TARGET(...)` 的行序。
4. **预期结果**：行序与 `LIBOMPTARGET_PLUGINS_TO_BUILD` 展开后一致；`host` 永远在列表里（u1-l2 讲过它会强制追加）。

### 4.3 OpenMP 设备号到「插件 + 设备」的双向映射（核心）

这是本讲的重头戏，也是实践任务所在。

#### 4.3.1 概念说明：为什么要双向映射

OpenMP 给用户的是一个**连续、从 0 开始**的整数设备号（`device(0)`、`device(1)`…）。但底层硬件是按「插件 + 插件内编号」组织的。所以需要两张表：

- **正向**：给「这个插件的这块卡」分配一个 UserId。
- **反向**：给「用户要的 UserId」找回它属于哪个插件、插件内是第几号。

正向表是 `DeviceIds`（`DenseMap`），反向表则是每个 `DeviceTy` 对象自带的三个字段：

[include/device.h:47-52](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h#L47-L52) —— `DeviceID`（即 UserId）、`RTL`（指向所属 `GenericPluginTy`）、`RTLDeviceID`（插件内设备号）。有了 `RTL` 和 `RTLDeviceID`，任何上层调用都能转发到正确的插件和硬件。

#### 4.3.2 核心流程：编号是如何分配的

编号在 `initializeDevice` 里按「插件优先、设备次之」的顺序连续分配：

[libomptarget/PluginManager.cpp:87-121](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L87-L121) —— 关键三步：

1. **取号**（第 100 行）：`int32_t UserId = ExclusiveDevicesAccessor->size();`——直接用「当前设备数组的长度」当作下一个 UserId。因为长度就是「已分配出去的个数」，天然连续。
2. **建对象**（第 107 行）：`auto Device = std::make_unique<DeviceTy>(&Plugin, UserId, DeviceId);`——把插件指针、UserId、插件内 DeviceId 一起塞进 `DeviceTy`，反向信息就此固化。
3. **登记正向表**（第 118 行）：`PM->DeviceIds[std::make_pair(&Plugin, DeviceId)] = UserId;`——写回 `DeviceIds` 映射。

而驱动这个分配的循环在 `initializeAllDevices`：

[libomptarget/PluginManager.cpp:123-140](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L123-L140) —— 外层按 `plugins()` 顺序遍历插件并 `initializePlugin`；内层对该插件的 `0..number_of_devices()` 逐个 `initializeDevice`。末尾还通过 `std::atexit` 注册了一个 Interop 清理回调（必须在插件 deinit 之前清理，因为 native 库可能已被卸载）。

由此可写出编号公式。设插件按顺序为 \(P_0, P_1, \dots\)，插件 \(P_k\) 实际初始化的设备数为 \(n_k\)，则 \(P_k\) 内第 \(j\) 号设备的 UserId 为：

\[
\text{UserId}(P_k, j) = \left(\sum_{i=0}^{k-1} n_i\right) + j
\]

即「排在它前面的所有插件设备数之和」加上「它在插件内的序号」。这正是「数组长度当 UserId」的等价表述。

反向查询入口是 `getDevice`：

[libomptarget/PluginManager.cpp:553-573](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L553-L573) —— 越界则返回 `llvm::Expected` 错误；否则取 `(*Devices)[DeviceNo]`，并在「有待加载镜像」时触发 `loadImagesOntoDevice`（见 4.5）。注意它返回的是 `DeviceTy&`，调用方据此拿到 `RTL` 和 `RTLDeviceID` 完成转发。

设备总数由 `getNumDevices` 给出：

[include/PluginManager.h:114-115](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L114-L115) —— 直接返回设备数组的长度，与 UserId 的取号口径完全一致。

#### 4.3.3 双向映射伪代码

把上面的源码浓缩成伪代码（**示例代码**，非项目原文）：

```text
// 正向：(插件, 插件内设备号) -> UserId
function initializeDevice(Plugin, DeviceId):
    UserId = Devices.size()              // 取号 = 当前长度
    dev = new DeviceTy(RTL=Plugin, DeviceID=UserId, RTLDeviceID=DeviceId)
    Devices.push(dev)                    // 反向信息随对象固化
    DeviceIds[(Plugin, DeviceId)] = UserId  // 写正向表

// 反向：UserId -> (插件, 插件内设备号)
function getDevice(UserId):
    dev = Devices[UserId]                // 越界 -> 返回错误
    return dev                           // 调用方用 dev.RTL / dev.RTLDeviceID 转发
```

#### 4.3.4 代码实践：追踪设备编号分配（本讲主实践）

> 这正是任务规格里要求的实践。

1. **实践目标**：用自己的话说明多插件环境下设备编号如何分配，并用伪代码描述双向映射。
2. **操作步骤**：
   - 依次阅读 [initializeAllDevices](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L123-L140)（L123-140）、[initializeDevice](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L87-L121)（L87-121）、[getDevice](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L553-L573)（L553-573）。
   - 写下：UserId 在哪里取值？反向信息存在哪？正向表何时写？
   - 可选的运行观测：编译一个最小 `target parallel for` 程序，用 `LIBOMPTARGET_INFO=63` 运行（参见 [u1-l4](u1-l4-toolchain-and-run.md) 对该环境变量的说明），观察日志里类似 `Registered plugin cuda with 2 visible device(s)` 的行。
3. **需要观察的现象**：插件按 `Targets.def` 顺序出现；每个插件报告的设备数累加起来应等于 `omp_get_num_devices()` 的返回值。
4. **预期结果**：若本机只有 host 插件可用，你会看到 `Registered plugin host with N visible device(s)`，N 通常等于主机逻辑 CPU 数；`omp_get_num_devices()` 返回 N，且 `device(0)..device(N-1)` 都能成功。
5. 若你无法在本机构建运行，**待本地验证**——但源码阅读部分（写出伪代码与编号公式）不依赖运行即可完成。

#### 4.3.5 小练习与答案

**练习 1**：如果插件 A（2 设备）排在插件 B（1 设备）之前，B 的那块卡 UserId 是多少？

> **答案**：2。A 的两块卡先被初始化，分别拿到 UserId 0、1（取号时 `Devices.size()` 分别是 0、1）；轮到 B 的卡时 `Devices.size()` 已经是 2，所以 UserId = 2。注意 UserId 从 0 起算，所以「第 3 个被 push 的设备」其编号是 2，不是 3。

**练习 2**：为什么 `initializeDevice` 用 `Devices.size()` 当 UserId，而不是维护一个自增计数器？

> **答案**：因为 `Devices` 数组本身就是「按 UserId 顺序排列」的，数组下标 == UserId。用长度当下一个 UserId 保证了「下标与 UserId 永远一致」，反向查询 `Devices[UserId]` 才能成立。自增计数器一旦与数组脱钩（例如未来支持设备移除），这套 O(1) 反向查询就会失效。

### 4.4 设备容器的独占访问与并发保护

#### 4.4.1 概念说明：为什么设备访问要加锁

OpenMP 程序常常是多线程的：多个主机线程可能同时 `target enter data`、同时 `getDevice(i)`。`Devices` 是一个会被「读取 + 追加」的共享容器，必须防止「一个线程正在 push 新设备，另一个线程同时按 UserId 读」导致的数据竞争。

`PluginManager` 用 `ProtectedObj` / `Accessor` 这对工具把「对象 + 锁」打包，靠 RAII（构造时加锁、析构时解锁）保证安全。

#### 4.4.2 核心流程：`ProtectedObj` 与 `Accessor`

[include/ExclusiveAccess.h:26-38](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/ExclusiveAccess.h#L26-L38) —— `ProtectedObj<Ty>` 内部持有一个 `Ty Obj` 和一个 `std::mutex Mtx`，唯一访问途径是 `getExclusiveAccessor()`。

[include/ExclusiveAccess.h:41-92](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/ExclusiveAccess.h#L41-L92) —— `Accessor` 在构造时 `lock()`、析构时 `unlock()`，并通过 `operator*` / `operator->` 透明地访问底层数据。它**可移动、不可拷贝**（拷贝构造被 `delete`），从而保证「同一时刻只有一个 Accessor 持有锁」。

使用方式很简洁：

[include/PluginManager.h:114-120](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L114-L120) —— `getNumDevices` 与 `getExclusiveDevicesAccessor` 都从 `Devices`（一个 `ProtectedObj<DeviceContainerTy>`）取访问器。

一个真实调用点：

[libomptarget/PluginManager.cpp:97-114](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L97-L114) —— `initializeDevice` 拿到 `ExclusiveDevicesAccessor` 后，`->size()`、`->push_back(...)` 都在锁保护下完成。Accessor 是局部变量，函数返回时自动解锁。

#### 4.4.3 运行时的「三把锁」

除设备容器外，`PluginManager` 还有三把显式互斥锁，各守一张表：

| 锁 | 保护对象 | 典型持有者 |
| --- | --- | --- |
| `RTLsMtx` | 整个 `registerLib` / `unregisterLib` 流程 | [registerLib](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L197-L301)（L197、L301） |
| `TrlTblMtx` | 翻译表 `HostEntriesBeginToTransTable` | registerLib 内部（L262、L295）；`loadImagesOntoDevice`（L397） |
| `TblMapMtx` | 主机指针→表项映射 `HostPtrToTableMap` | unregisterLib（L364、L384） |

注意 `registerLib` 持有 `RTLsMtx` 期间**还会再获取 `TrlTblMtx`**（L262）——这是一种**锁序（lock ordering）**。理解锁序在排查死锁时很有用：任何地方若反向先拿 `TrlTblMtx` 再拿 `RTLsMtx`，就可能死锁。

> 为什么 `getDevice` 只在取下标那一段加锁（L556-564），拿到 `DeviceTy&` 后就解锁（L565 之后）？因为返回的是**引用**——`DeviceContainerTy` 用 `unique_ptr` 存设备，正是为了保证「容器变化时设备地址仍然稳定」，这样调用方在锁外持有引用也安全。源码注释点明了这一点：[include/PluginManager.h:44-46](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L44-L46)。

#### 4.4.4 代码实践：核对锁的获取顺序

1. **实践目标**：确认 `registerLib` 中 `RTLsMtx` 与 `TrlTblMtx` 的嵌套顺序。
2. **操作步骤**：在 [PluginManager.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L196-L317) 中搜索 `RTLsMtx.lock()` 与 `TrlTblMtx.lock()`，画出嵌套关系。
3. **预期结果**：`RTLsMtx` 在外、`TrlTblMtx` 在内；全程没有「先 `TrlTblMtx` 后 `RTLsMtx`」的反向获取。

### 4.5 Requirements 收集、延迟注册与延迟加载

这三个机制都挂在「注册 / 初始化」这条主线上，放在一起讲。

#### 4.5.1 概念说明

- **Requirements**：OpenMP 的 `omp requires` 指令（如 `unified_shared_memory`、`reverse_offload`）是**全局约束**，必须在程序启动前确定，且不同翻译单元之间必须一致。运行时需要在注册期把这些标志收集起来，并在用到时（如加载全局变量时判断是否走统一内存）查询。
- **延迟注册（delayed registration）**：某些插件在自身 `init()` 时会 `dlopen` 一个共享库，而该库的构造函数可能反过来调用 `__tgt_register_lib`。此时外层的 `registerLib` 还没走完，存在**重入时序竞态**。解决办法是「先暂存、后重放」。
- **延迟加载（lazy load）**：注册期只把镜像**登记**进翻译表，真正的 `loadBinary` 推迟到该设备**首次被 `getDevice` 取用时**才做（这一点 [u2-l1](u2-l1-runtime-entry.md) 已点明，本讲给出它的源码位置）。

#### 4.5.2 核心流程一：Requirements 的收集与一致性

收集发生在 `registerLib` 开头，扫描所有主机条目里带 `OMP_REGISTER_REQUIRES` 标志的项：

[libomptarget/PluginManager.cpp:202-207](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L202-L207) —— 遍历 `HostEntriesBegin..HostEntriesEnd`，把每个 requires 条目的 `Entry.Data` 通过 `PM->addRequirements(...)` 累加进 `Requirements`。

一致性检查在 `RequirementCollection::addRequirements`：

[include/Shared/Requirements.h:60-95](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h#L60-L95) —— 规则是「第一次调用直接设置；后续调用必须与已设值逐位一致」，否则 `FATAL_MESSAGE` 直接终止。特例：`OMPX_REQ_AUTO_ZERO_COPY` 允许在已设为 `OMP_REQ_NONE` 时被覆盖（见下）。

标志定义见 [include/Shared/Requirements.h:24-42](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h#L24-L42)。

APU（如某些集成 GPU 的片上系统）上还有一个**自动**特性：注册结束后，看 0 号设备是否支持 Auto Zero-Copy，若是则自动追加该需求：

[libomptarget/PluginManager.cpp:303-314](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L303-L314) —— 取第一个设备，若 `Device.useAutoZeroCopy()` 为真，则 `addRequirements(OMPX_REQ_AUTO_ZERO_COPY)`。之所以只看 0 号设备，是因为注释说「APU 是同质 GPU 集合」。

#### 4.5.3 核心流程二：延迟注册的「暂存 + 重放」

[include/PluginManager.h:99-112](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L99-L112) —— `delayRegisterLib`：若 `RTLsLoaded` 还是 false（插件尚未全部初始化），就把 `Desc` 暂存进 `DelayedBinDesc` 并返回 true（让调用方提前返回）。`registerDelayedLibraries`：置 `RTLsLoaded = true`，再把暂存的所有 `Desc` 逐个交给真正的 `__tgt_register_lib` 重放，最后清空暂存区。

调用链串起来看：

- `__tgt_register_lib`（[interface.cpp:91-97](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L91-L97)）先 `initRuntime()`，再尝试 `delayRegisterLib`；若被暂存则直接返回，否则走真正的 `PM->registerLib`。
- `initRuntime`（[OffloadRTL.cpp:38-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OffloadRTL.cpp#L38-L62)）在首次进入时调用 `PM->init()` 紧接着 `PM->registerDelayedLibraries()`——也就是说，「插件全部构造完成」这一刻被选为重放暂存库的时机，正好化解了「插件 init 期间重入注册」的竞态。

#### 4.5.4 核心流程三：延迟加载 `loadImagesOntoDevice`

[libomptarget/PluginManager.cpp:390-551](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L390-L551) —— 这个 `static` 函数在设备**首次使用**时被 `getDevice` 调用（见 [PluginManager.cpp:567-571](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L567-L571)）。它做三件事：①对翻译表里该 `DeviceId` 对应的镜像调 `Device.loadBinary`；②遍历镜像条目，把全局变量/内核符号解析成设备地址，建立 `__tgt_target_table`；③为每个 `declare target` 全局变量在主机↔设备间建立初始映射（必要时回读间接指针）。

注意它如何与 Requirements 联动：加载全局变量时会查询 `PM->getRequirements() & OMP_REQ_UNIFIED_SHARED_MEMORY`（或 `OMPX_REQ_AUTO_ZERO_COPY`）来决定是否把主机地址直接写进设备（统一内存语义）——见 [PluginManager.cpp:452-459](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L452-L459)。

> 「待加载」状态由 `DeviceTy::hasPendingImages()` 表达，在 `initializeDevice` 已初始化分支里被置为 true（[PluginManager.cpp:89-95](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L89-L95)），在 `loadImagesOntoDevice` 末尾被清掉（L539）。这就闭环了「注册期登记、使用期加载」。

#### 4.5.5 小练习与答案

**练习 1**：如果两个翻译单元分别写了 `omp requires unified_shared_memory` 和没写，运行时会发生什么？

> **答案**：第一个 TU 注册时把 `OMP_REQ_UNIFIED_SHARED_MEMORY` 写入 `SetFlags`；第二个 TU 注册时 `addRequirements` 发现该位不一致，`checkConsistency` 触发 `FATAL_MESSAGE`，程序终止。这强制了 requires 子句的全局一致性。

**练习 2**：为什么「重放暂存库」的时机选在 `initRuntime` 里 `PM->init()` 之后？

> **答案**：`PM->init()` 完成意味着所有插件对象都已构造。那些在自身 `init` 中 `dlopen` 触发的、重入的 `__tgt_register_lib` 此时都已安全地暂存在 `DelayedBinDesc` 里。紧接着调用 `registerDelayedLibraries` 重放它们，既避免了在 `registerLib` 执行中途重入的竞态，又保证这些库不会漏注册。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个**端到端追踪任务**：

**场景**：假设一台机器装了「2 块 NVIDIA GPU（cuda 插件）+ 主机 CPU（host 插件）」，且 `Targets.def` 里 cuda 排在 host 之前。

**任务**：

1. 推算每块「设备」的 OpenMP 设备号（UserId），并写出 `DeviceIds` 正向表与各 `DeviceTy` 的 `(RTL, RTLDeviceID)` 反向字段。用 4.3.3 的伪代码格式表达。
2. 说明当用户调用 `target map(to:a) device(2)` 时，运行时如何从 `getDevice(2)` 一路找到「正确的插件与硬件」。标出经过的源码行（`getDevice` → `DeviceTy` 的 `RTL`/`RTLDeviceID` 字段）。
3. 解释若此时是程序里**第一次**使用 2 号设备，`getDevice` 会多做哪一步（提示：`hasPendingImages` → `loadImagesOntoDevice`），以及为何这一步要放在使用期而不是注册期。
4. （可选运行验证）在有 GPU 的机器上用 `LIBOMPTARGET_INFO=63` 跑一个 OpenMP 卸载程序，把日志里 `Registered plugin ... with N visible device(s)` 的行与你的推算对齐；在只有 CPU 的机器上则用 host 插件做同样的核对。

**交付物**：一段文字 + 一张「UserId ↔ (插件, 插件内号) ↔ 设备」的对照表 + `getDevice` 调用链的源码行号标注。

---

## 6. 本讲小结

- `PluginManager` 是 libomptarget 的**调度中枢**：向下持有异构插件，向上把它们展平成连续编号的 `DeviceTy`；全局单例由 `PM` 暴露。
- 插件列表是**构建期**用 X-macro（`Targets.def`）决定的，`init()` 只构造对象，`initializePlugin` 才真正探测硬件；两者分离使「构造 ≠ 初始化」。
- **设备编号**在 `initializeDevice` 中用「数组当前长度」当 UserId 连续分配，正向表 `DeviceIds` 与反向载体 `DeviceTy{RTL, RTLDeviceID}` 构成双向映射；编号公式为 \(\text{UserId}(P_k,j)=\sum_{i<k}n_i + j\)。
- 设备容器用 `ProtectedObj` / `Accessor`（RAII 锁）做独占访问；运行时另有 `RTLsMtx`、`TrlTblMtx`、`TblMapMtx` 三把锁，存在「外层 `RTLsMtx`、内层 `TrlTblMtx`」的锁序。
- `RequirementCollection` 在注册期收集 `omp requires` 标志并强制跨翻译单元一致性；APU 上还会自动追加 `OMPX_REQ_AUTO_ZERO_COPY`。
- 「延迟注册」（`delayRegisterLib`/`registerDelayedLibraries`）化解插件 init 期间重入注册的竞态；「延迟加载」（`loadImagesOntoDevice`）把镜像 `loadBinary` 推迟到设备首次使用——共同构成「注册期登记、使用期加载」的按需设计。

## 7. 下一步学习建议

- 下一篇 [u2-l3 DeviceTy 设备抽象](u2-l3-device-abstraction.md) 会钻进 `DeviceTy` 内部，看它如何把上层的 `allocData`/`submitData`/`launchKernel` **转发**到 `GenericPluginTy` 的底层接口——也就是本讲反向映射找到 `RTL`/`RTLDeviceID` 之后，调用究竟是怎么发出去的。
- 想提前了解「插件一侧」如何实现 `isPluginCompatible`/`isDeviceCompatible`/`init`，可先扫一眼 [u3-l1 通用插件接口](u3-l1-plugin-interface.md)。
- 想理解 `loadImagesOntoDevice` 里建立的主机↔设备映射如何被后续 `target data` 复用，可接着读 [u2-l4 主机-设备数据映射机制](u2-l4-data-mapping.md)。
