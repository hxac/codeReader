# omp_target_* 用户 API 与显式内存分配

## 1. 本讲目标

前面几讲（u2-l1 ~ u2-l7）讲的都是「编译器生成 `__tgt_*` 调用、运行时自动搬运数据、自动启动内核」这条**隐式**链路。本讲把视角切到**用户面**：OpenMP 5.x 规范定义了一组 `omp_target_*` C 接口，让用户程序**绕过 `map` 子句、手动控制设备内存**。

学完本讲，你应该能够：

1. 说清 `omp_target_alloc` / `omp_target_free` 是如何分配和释放设备内存的，以及三种显式分配器（device/host/shared）的区别。
2. 读懂 `omp_target_memcpy` 的「四向分发」逻辑：主机↔主机、主机↔设备、设备↔主机、设备↔设备。
3. 用 `omp_target_is_present` / `omp_target_is_accessible` / `omp_target_associate_ptr` / `omp_get_mapped_ptr` 查询和操作主机-设备映射表。
4. 理解「设备号（DeviceNum）」与「设备 UID」两种设备标识的转换关系。

本讲承接 u2-l3（`DeviceTy` 设备抽象）：你会看到这些用户面 API 最终都汇聚到 `DeviceTy` 的 `allocData` / `submitData` / `getMappingInfo()` 等方法上，由 `DeviceTy` 这个 Facade 再转发给具体插件。

## 2. 前置知识

### 2.1 为什么需要显式内存 API

OpenMP 的 `map(to:)` / `map(from:)` 子句用起来很省心：进入 `target data` 区域时运行时自动把数据搬上设备，退出时再搬回来。但它有局限：

- **生命周期绑定区域**：数据搬运发生在进入/退出区域的那一刻，无法在任意时刻搬运。
- **指针不透明**：用户拿不到「这段主机数据在设备上对应的真实地址」，只能靠 `map` 隐式管理。
- **无法手动管理设备显存**：GPU 上有 device memory（显存）、host-pinned memory、shared/unified memory 等多种内存空间，`map` 子句无法精确选择。

`omp_target_*` 这组 API 就是为了补齐这些能力，常用于：

- 与其他运行时（CUDA、HIP、Level Zero）互操作时直接交换设备指针。
- 实现自定义的内存池或缓存。
- 在 OpenMP 区域外做数据预取。

### 2.2 两个必须先分清的标识

阅读本讲源码时，你会反复看到两个判断：

- **设备号（DeviceNum）**：一个整数，是用户/编译器视角的设备下标。`omp_get_num_devices()` 返回设备总数 `N`，合法的非主机设备号是 `0..N-1`。
- **主机设备（initial device）**：主机本身也被当作一个「设备」。它的设备号有两种等价写法——`omp_get_initial_device()`（其值等于 `N`）和常量 `omp_initial_device`（值为 `-1`）。

运行时里有一个内联函数专门把这两种写法统一判断：

```cpp
// libomptarget/private.h
static inline bool isInitialDevice(const int &DeviceNum) {
  return DeviceNum == omp_get_initial_device() ||
         DeviceNum == omp_initial_device;
}
```

也就是说，凡是用 `-1` 或 `N` 作为设备号传入的，运行时都识别为「在主机上操作」，走主机快速路径（直接 `malloc` / `memcpy`），完全不碰插件。

> **提示**：`omp_initial_device` 和 `omp_invalid_device` 这两个常量定义在 [include/OpenMP/omp.h:37-38](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/omp.h#L37-L38)，值分别是 `-1` 和 `-2`。

### 2.3 本讲用到的两个返回码

[include/omptarget.h:31-32](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L31-L32) 定义了贯穿全运行时的两个状态码：

```cpp
#define OFFLOAD_SUCCESS (0)
#define OFFLOAD_FAIL (~0)
```

注意 `omp_target_memcpy` 这类函数返回 `int`（0 成功、非 0 失败），而 `omp_target_alloc` 返回 `void*`（失败返回 `NULL`），`omp_target_is_present` 返回 `int`（0/1 当布尔值用）。每个 API 的返回语义不同，读源码时要留意。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [libomptarget/OpenMP/API.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp) | **本讲主文件**。所有 `omp_target_*` / `omp_get_*` 用户面 API 的实现入口。 |
| [libomptarget/omptarget.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp) | 提供 `targetAllocExplicit` / `targetFreeExplicit` / `targetLockExplicit` 等内部 helper，被 API.cpp 复用。 |
| [libomptarget/private.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/private.h) | 内部头文件，定义 `isInitialDevice()` 等小工具。 |
| [include/omptarget.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h) | 契约头文件，定义 `TargetAllocTy` 枚举、`OFFLOAD_SUCCESS/FAIL`、`AsyncInfoTy`。 |
| [include/device.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/device.h) | `DeviceTy` 的接口声明（`allocData`/`deleteData`/`isAccessiblePtr`）。 |
| [libomptarget/device.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp) | `DeviceTy` 的实现，本讲关注它如何把请求转发给插件。 |
| [plugins-nextgen/common/include/PluginInterface.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h) | 插件框架基类，定义 `getHostDeviceUid()`、`getDeviceUid()`。 |

> **关于 `include/OpenMP/omp.h` 的一点澄清**：规格里把它列为关键源码，但它**并不是** `omp_target_alloc` 这些函数的声明出处。看文件头注释就知道，它是「Copies of OpenMP user facing types and APIs for easy reach within the implementation」——一份**给实现内部方便引用的副本**，只拷贝了 `omp_initial_device` 常量、`omp_depend_t`、Interop 类型等少量内容。`omp_target_*` 的真正函数原型声明在 OpenMP 运行时（libomp）随编译器分发的标准 `omp.h` 里。本讲引用 `include/OpenMP/omp.h` 仅用于说明常量定义。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. **4.1 设备标识体系**：设备号、主机设备、设备 UID 三层标识。
2. **4.2 显式内存分配与释放**：`omp_target_alloc/free` 与三种分配器 Kind。
3. **4.3 数据搬运 `omp_target_memcpy`**：四向分发与异步 helper task。
4. **4.4 映射查询 API**：`is_present` / `is_accessible` / `associate_ptr` / `get_mapped_ptr`。

### 4.1 设备标识体系：DeviceNum、initial device 与 UID

#### 4.1.1 概念说明

在多设备环境下，用户需要一个稳定的方式来「点名」某台设备。libomptarget 提供了**两种并列的标识**，服务于不同场景：

| 标识 | 类型 | 特点 | 适用场景 |
|------|------|------|----------|
| **DeviceNum**（设备号） | `int` | 从 0 开始的连续下标，**不稳定**（插拔设备、改构建选项都会变） | 写在 `map` 子句、`device()` 子句、`omp_target_*` 参数里 |
| **UID**（设备唯一标识） | `const char*` 字符串 | 形如 `"cuda-<vendor_uid>"`，跨进程/跨运行稳定 | 持久化记录、跨主机比较设备身份 |

DeviceNum 虽然方便但「脆弱」——它在 u2-l2 里讲过，是由 `PluginManager` 按「插件顺序 + 插件内设备顺序」连续编号的。如果机器上多了一张显卡，所有编号都可能移位。UID 则是设备硬件层面（厂商 UID）加上插件名前缀，更稳定。

主机本身在这两套体系里都有特殊地位：

- DeviceNum 体系下：主机设备号 = `omp_get_initial_device()` = 设备总数 `N`（也接受 `-1`）。
- UID 体系下：主机的 UID 是固定字符串 `"HOST"`。

#### 4.1.2 核心流程

设备号查询的调用链非常薄：

```
omp_get_num_devices()   →  PM->getNumDevices()          // 非主机设备数 N
omp_get_initial_device() →  omp_get_num_devices()        // 主机号 = N
omp_get_device_num()    →  omp_get_initial_device()      // 当前主机线程所在设备 = 主机
```

UID 与设备号互转则是「线性扫描 + 字符串比较」：

```
omp_get_uid_from_device(DeviceNum):
  if DeviceNum == 初始设备  → 返回 "HOST"
  否则 PM->getDevice(DeviceNum) → 读 Device.RTL->getDevice(RTLDeviceID).getDeviceUid()

omp_get_device_from_uid(UID):
  if UID == "HOST"          → 返回 omp_get_initial_device()
  否则遍历所有设备，逐个比较 getDeviceUid() == UID，命中则返回 Device.DeviceID
```

UID 的构造规则在插件框架里统一生成：

\[ \text{UID} = \text{PluginName} \, +\, \text{"-"} \, +\, \text{VendorUid} \]

例如 CUDA 插件的某张卡可能是 `"cuda-0"`，Level Zero 的某核显可能是 `"level_zero-0"`。主机的 UID 是特例，固定为 `"HOST"`。

#### 4.1.3 源码精读

**设备号查询三件套**（[libomptarget/OpenMP/API.cpp:55-73](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L55-L73)）：

```cpp
EXTERN int omp_get_num_devices(void) {
  TIMESCOPE();
  size_t NumDevices = PM->getNumDevices();   // 非主机设备总数
  return NumDevices;
}

EXTERN int omp_get_device_num(void) {
  int HostDevice = omp_get_initial_device();  // 当前线程在主机上
  return HostDevice;
}
```

`omp_get_initial_device` 紧接其后（[API.cpp:134-140](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L134-L140)），直接返回 `omp_get_num_devices()`——**主机的设备号就是设备总数**，这是一个容易让人困惑的设计：假设有 2 张 GPU，那么设备 0、1 是 GPU，设备 2（= `N`）和 `-1` 都代表主机。

**UID → 设备号**（[API.cpp:79-108](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L79-L108)）：

```cpp
EXTERN int omp_get_device_from_uid(const char *DeviceUid) {
  if (!DeviceUid)
    return omp_invalid_device;            // -2
  if (is_initial_device_uid(DeviceUid))   // UID == "HOST"
    return omp_get_initial_device();

  int DeviceNum = omp_invalid_device;
  auto ExclusiveDevicesAccessor = PM->getExclusiveDevicesAccessor();  // 持锁遍历
  for (const DeviceTy &Device : PM->devices(ExclusiveDevicesAccessor)) {
    const char *Uid = Device.RTL->getDevice(Device.RTLDeviceID).getDeviceUid();
    if (Uid && strcmp(DeviceUid, Uid) == 0) {
      DeviceNum = Device.DeviceID;        // 命中，返回该设备的 OpenMP 设备号
      break;
    }
  }
  return DeviceNum;
}
```

注意这里用 `getExclusiveDevicesAccessor()` 拿到设备数组的独占访问权（u2-l2 讲过的 RAII 锁），然后线性扫描所有设备比较 UID。

**设备号 → UID**（[API.cpp:110-132](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L110-L132)）走的是精确取设备而非扫描：

```cpp
EXTERN const char *omp_get_uid_from_device(int DeviceNum) {
  if (DeviceNum == omp_invalid_device) return nullptr;
  if (isInitialDevice(DeviceNum))
    return GenericPluginTy::getHostDeviceUid();   // "HOST"
  auto DeviceOrErr = PM->getDevice(DeviceNum);    // 直接按下标取
  ...
  return DeviceOrErr->RTL->getDevice(DeviceOrErr->RTLDeviceID).getDeviceUid();
}
```

**UID 的来源**。主机 UID 是个编译期常量（[PluginInterface.h:1514](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L1514)）：

```cpp
static constexpr const char *getHostDeviceUid() { return "HOST"; }
```

非主机设备的 UID 在插件初始化时由「插件名 + 厂商 UID」拼接（[PluginInterface.cpp:1323](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/src/PluginInterface.cpp#L1323)）：

```cpp
DeviceUid = std::string(Plugin.getName()) + "-" + std::string(VendorUid);
```

`getDeviceUid()` 只是个简单 getter（[PluginInterface.h:920](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/plugins-nextgen/common/include/PluginInterface.h#L920)），返回内部 `DeviceUid` 字符串的 C 字符串。

**`isInitialDevice` 的统一定义**（[libomptarget/private.h:94-97](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/private.h#L94-L97)）：

```cpp
static inline bool isInitialDevice(const int &DeviceNum) {
  return DeviceNum == omp_get_initial_device() ||
         DeviceNum == omp_initial_device;   // N 或 -1
}
```

#### 4.1.4 代码实践

**实践目标**：直观感受 DeviceNum 与 UID 的关系。

**操作步骤**：

1. 写一个小程序 `uid_probe.c`：

   ```c
   // 示例代码
   #include <stdio.h>
   #include <omp.h>

   int main(void) {
       int ndev = omp_get_num_devices();
       printf("num_devices       = %d\n", ndev);
       printf("initial_device    = %d\n", omp_get_initial_device());
       printf("host uid          = %s\n",
              omp_get_uid_from_device(omp_get_initial_device()));
       for (int d = 0; d < ndev; ++d) {
           const char *uid = omp_get_uid_from_device(d);
           int back = omp_get_device_from_uid(uid);
           printf("device %d -> uid '%s' -> back %d\n", d, uid, back);
       }
       return 0;
   }
   ```

2. 编译并运行（host 插件即可，无需 GPU）：

   ```bash
   clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu uid_probe.c -o uid_probe
   ./uid_probe
   ```

**需要观察的现象**：

- `num_devices` 至少为 1（host 插件把自己注册为一个非主机设备）。
- `initial_device` 等于 `num_devices`。
- host 的 UID 是 `"HOST"`。
- 对每个设备，`d -> uid -> back` 中的 `back` 应该等于 `d`，验证 UID 与 DeviceNum 的双向转换自洽。

**预期结果**：每个设备的往返转换 `omp_get_device_from_uid(omp_get_uid_from_device(d)) == d` 成立。具体 UID 字符串取决于你启用的插件（host 插件下形如 `"host-..."`）。如果本机无法构建/运行，**待本地验证**具体 UID 串。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `omp_get_device_from_uid` 要用 `getExclusiveDevicesAccessor()` 加锁，而 `omp_get_uid_from_device` 只用 `PM->getDevice()`？

**参考答案**：前者需要**遍历整个设备数组**（读 `PM->devices()`），访问期间数组不能被并发修改（如延迟加载新设备），所以要持独占锁；后者是按下标**精确取单个设备**，`PM->getDevice(DeviceNum)` 内部已自带必要的同步，无需外层遍历锁。

**练习 2**：假设系统上有 3 张 GPU，`omp_get_initial_device()` 返回多少？把 `-1` 作为设备号传给 `omp_target_alloc` 会发生什么？

**参考答案**：返回 `3`。`-1` 等价于 `omp_initial_device`，`isInitialDevice(-1)` 为真，`omp_target_alloc` 会走主机快速路径，直接用 `malloc` 在主机上分配，返回一个普通主机指针，完全不调用插件。

---

### 4.2 显式内存分配与释放：alloc/free 与分配器 Kind

#### 4.2.1 概念说明

`omp_target_alloc(Size, DeviceNum)` 在指定设备上分配 `Size` 字节，返回一个**设备指针**。它和 `map` 子句的关键区别是：

- **不建立映射**：分配出来的设备指针不会出现在 u2-l4 讲的 `HDTTMap` 映射表里，运行时不跟踪它的生命周期。
- **不自动搬运**：你要自己用 `omp_target_memcpy` 把数据搬上搬下。
- **用户全权负责**：必须用 `omp_target_free` 显式释放，忘了就泄漏。

除了基础的 `omp_target_alloc`（OpenMP 标准），LLVM 还扩展了三个**显式分配器**，用来精确选择内存空间：

| API | Kind 常量 | 语义 |
|-----|----------|------|
| `omp_target_alloc` | `TARGET_ALLOC_DEFAULT` | 让运行时/插件自己选（通常等价于 device） |
| `llvm_omp_target_alloc_device` | `TARGET_ALLOC_DEVICE` | 设备专用显存（GPU 上最快，主机不可直接访问） |
| `llvm_omp_target_alloc_host` | `TARGET_ALLOC_HOST` | 主机侧 pinned memory（可被设备 DMA 高效访问） |
| `llvm_omp_target_alloc_shared` | `TARGET_ALLOC_SHARED` | 主机与设备共享的统一内存（unified/shared memory） |

这套 Kind 枚举定义在 [include/omptarget.h:105-111](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L105-L111)：

```cpp
enum TargetAllocTy : int32_t {
  TARGET_ALLOC_DEVICE = 0,
  TARGET_ALLOC_HOST,
  TARGET_ALLOC_SHARED,
  TARGET_ALLOC_DEFAULT,
  TARGET_ALLOC_LAST = TARGET_ALLOC_DEFAULT
};
```

注意 `DEFAULT` 被故意放在最后（`LAST`），这样插件可以用 `Kind <= TARGET_ALLOC_LAST` 做范围校验。不同插件对同一个 Kind 的物理实现完全不同——host 插件对任何 Kind 都只是 `malloc`；GPU 插件则会映射到不同的内存池（这部分细节留到 u3-l6 设备内存管理器）。

#### 4.2.2 核心流程

`omp_target_*_alloc` 一族函数都走同一个内部 helper `targetAllocExplicit`，流程是：

```
omp_target_alloc(Size, DeviceNum)
  └─ targetAllocExplicit(Size, DeviceNum, TARGET_ALLOC_DEFAULT, __func__)
       ├─ if Size <= 0           → 返回 NULL
       ├─ if isInitialDevice(D)  → malloc(Size) 直接在主机分配      ← 主机快速路径
       └─ 否则:
            PM->getDevice(DeviceNum)
            Device.allocData(Size, nullptr, Kind)
              └─ DeviceTy::allocData → RTL->data_alloc(RTLDeviceID, Size, HstPtr, Kind)
                    └─ 插件按 Kind 在对应内存空间分配
```

释放路径 `targetFreeExplicit` 对称：

```
omp_target_free(Ptr, DeviceNum)
  └─ targetFreeExplicit(Ptr, DeviceNum, TARGET_ALLOC_DEFAULT, __func__)
       ├─ if Ptr == NULL         → 直接返回（no-op）
       ├─ if isInitialDevice(D)  → free(Ptr)
       └─ 否则:
            Device.deleteData(Ptr, Kind)
              └─ DeviceTy::deleteData → RTL->data_delete(RTLDeviceID, Ptr, Kind)
            失败则 FATAL_MESSAGE 中止（并提示开启 OFFLOAD_TRACK_ALLOCATION_TRACES）
```

一个关键设计点：**Kind 必须匹配**。用 `llvm_omp_target_alloc_device` 分配的指针，必须用 `llvm_omp_target_free_device`（同样的 Kind）释放，因为某些后端按 Kind 维护不同的内存池，用错 Kind 释放会导致错误。

#### 4.2.3 源码精读

**用户面 API 只是个一行转发**（[API.cpp:158-199](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L158-L199)）：

```cpp
EXTERN void *omp_target_alloc(size_t Size, int DeviceNum) {
  TIMESCOPE_WITH_DETAILS("dst_dev=" + std::to_string(DeviceNum) + ...);
  return targetAllocExplicit(Size, DeviceNum, TARGET_ALLOC_DEFAULT, __func__);
}

EXTERN void *llvm_omp_target_alloc_device(size_t Size, int DeviceNum) {
  return targetAllocExplicit(Size, DeviceNum, TARGET_ALLOC_DEVICE, __func__);
}
// llvm_omp_target_alloc_host   → TARGET_ALLOC_HOST
// llvm_omp_target_alloc_shared → TARGET_ALLOC_SHARED
```

四个 alloc 函数、四个 free 函数，唯一区别就是传入的 `Kind`。这就是为什么把它们叫「显式分配器」——你显式指定内存空间，运行时原样转发。

**核心 helper `targetAllocExplicit`**（[libomptarget/omptarget.cpp:205-230](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L205-L230)）：

```cpp
void *targetAllocExplicit(size_t Size, int DeviceNum, int Kind, const char *Name) {
  if (Size <= 0) return NULL;                       // 1. 非法大小

  if (isInitialDevice(DeviceNum)) {                 // 2. 主机快速路径
    Rc = malloc(Size);
    return Rc;
  }

  auto DeviceOrErr = PM->getDevice(DeviceNum);      // 3. 设备路径
  ...
  Rc = DeviceOrErr->allocData(Size, nullptr, Kind); //    转给 DeviceTy
  return Rc;
}
```

`Name` 参数只是为了日志里打印调用者函数名（`omp_target_alloc` 还是 `llvm_omp_target_alloc_device`），因为多个 API 复用同一个 helper。

**释放 `targetFreeExplicit`**（[omptarget.cpp:232-258](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L232-L258)），注意失败处理比 alloc 严格得多：

```cpp
void targetFreeExplicit(void *DevicePtr, int DeviceNum, int Kind, const char *Name) {
  if (!DevicePtr) return;                           // NULL 指针直接返回

  if (isInitialDevice(DeviceNum)) { free(DevicePtr); return; }

  auto DeviceOrErr = PM->getDevice(DeviceNum);
  ...
  if (DeviceOrErr->deleteData(DevicePtr, Kind) == OFFLOAD_FAIL)
    FATAL_MESSAGE(DeviceNum, "%s",
                  "Failed to deallocate device ptr. Set "
                  "OFFLOAD_TRACK_ALLOCATION_TRACES=1 to track allocations.");
}
```

释放失败会**直接 `FATAL_MESSAGE` 中止进程**——因为内存泄漏或双重释放是致命错误，运行时选择 fail-fast，而不是返回错误码让程序带着损坏的状态继续跑。

**`DeviceTy` Facade 的转发**（[libomptarget/device.cpp:249-269](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L249-L269)），这正是 u2-l3 讲的「上层与插件之间的桥梁」：

```cpp
void *DeviceTy::allocData(int64_t Size, void *HstPtr, int32_t Kind) {
  void *TargetPtr = nullptr;
  OMPT_IF_BUILT(InterfaceRAII TargetDataAllocRAII(...));   // OMPT 工具回调
  TargetPtr = RTL->data_alloc(RTLDeviceID, Size, HstPtr, Kind);  // 逐字转发
  return TargetPtr;
}

int32_t DeviceTy::deleteData(void *TgtAllocBegin, int32_t Kind) {
  OMPT_IF_BUILT(InterfaceRAII TargetDataDeleteRAII(...));
  return RTL->data_delete(RTLDeviceID, TgtAllocBegin, Kind);
}
```

`DeviceTy` 自己不碰硬件，只做两件事：插入 OMPT 回调钩子（如果编译时启用了 OMPT），然后把请求连同 `Kind` 原样交给插件的 `RTL->data_alloc` / `data_delete`。`Kind` 就这样一路从用户的 `llvm_omp_target_alloc_shared` 传到 GPU 插件的内存分配器。

> 顺带一提 `omp_target_memset`（[API.cpp:463-502](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L463-L502)）：它对设备内存做填充，同样遵循「主机走 `memset`、设备走插件 `dataFill`」的二分。它没有返回错误码的能力（OpenMP 规范规定返回 `void*`），所以 `dataFill` 失败时只能静默忽略并在日志里抱怨。

#### 4.2.4 代码实践

**实践目标**：用 `omp_target_alloc` 分配设备内存，写入数据，再拷回主机验证，体会「显式分配不进入映射表」。

**操作步骤**：

1. 写程序 `explicit_alloc.c`（参考真实测试 [test/offloading/memory_manager.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/test/offloading/memory_manager.cpp)）：

   ```c
   // 示例代码
   #include <stdio.h>
   #include <omp.h>

   int main(void) {
       const int N = 1024;
       int dev = 0;                                  // 设备 0
       int *p = (int *)omp_target_alloc(N * sizeof(int), dev);
       if (!p) { printf("alloc failed\n"); return 1; }

       // p 是设备指针，必须用 is_device_ptr 告诉编译器别 map 它
   #pragma omp target teams distribute parallel for device(dev) is_device_ptr(p)
       for (int i = 0; i < N; ++i) p[i] = i;

       int buf[N];
   #pragma omp target teams distribute parallel for device(dev) map(from: buf) is_device_ptr(p)
       for (int i = 0; i < N; ++i) buf[i] = p[i];

       int ok = 1;
       for (int i = 0; i < N; ++i) if (buf[i] != i) ok = 0;
       printf("%s\n", ok ? "PASS" : "FAIL");

       omp_target_free(p, dev);                      // 必须显式释放
       return 0;
   }
   ```

2. 编译运行：

   ```bash
   clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu explicit_alloc.c -o explicit_alloc
   ./explicit_alloc
   ```

3. 开启信息输出再跑一次，观察分配/释放：

   ```bash
   LIBOMPTARGET_INFO=63 ./explicit_alloc 2>&1 | grep -i alloc
   ```

**需要观察的现象**：

- 必须给设备指针 `p` 加 `is_device_ptr` 子句。如果不加，编译器会尝试把 `p` 当普通主机变量 `map` 进去，导致语义错误。
- `LIBOMPTARGET_INFO` 输出里能看到 `omp_target_alloc` / `omp_target_free` 的调用记录，但**不会**看到对 `p` 的 H2D/D2H 数据搬运日志（因为显式分配不进映射表，`p` 本来就在设备上）。

**预期结果**：输出 `PASS`。如果本机没有可用设备或无法构建，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `omp_target_free` 失败时用 `FATAL_MESSAGE` 直接中止，而 `omp_target_alloc` 失败只是返回 `NULL`？

**参考答案**：`alloc` 失败是可恢复的常见情况（显存不足），返回 `NULL` 让用户决定如何降级处理；`free` 失败通常意味着双重释放或指针损坏，是严重的内存错误，继续运行会导致未定义行为，所以选择 fail-fast 立即中止，避免更难诊断的后续故障。

**练习 2**：`omp_target_alloc(ptr, omp_initial_device)` 和 `malloc(size)` 有什么区别？

**参考答案**：在本实现里**没有区别**——`isInitialDevice(-1)` 为真，`targetAllocExplicit` 直接调用 `malloc`。从语义上讲，前者是「在主机设备上分配」，后者是标准 C 分配，二者在这个运行时里殊途同归。

**练习 3**：用 `llvm_omp_target_alloc_shared` 分配的指针，释放时应该用哪个 free？

**参考答案**：应该用 `llvm_omp_target_free_shared`（`TARGET_ALLOC_SHARED`）。分配与释放的 Kind 必须一致，因为某些后端按 Kind 维护独立内存池，Kind 不匹配会导致释放到错误的池。

---

### 4.3 数据搬运：omp_target_memcpy 的四向分发

#### 4.3.1 概念说明

`omp_target_memcpy(dst, src, length, dst_offset, src_offset, dst_device, src_device)` 在两个设备之间拷贝 `length` 字节数据。它的强大之处在于：源设备和目标设备都可以是任意设备（包括主机），因此它实际上要处理**四种组合**。

`dst_offset` / `src_offset` 允许在缓冲区内部做偏移拷贝，省去用户手动算指针的麻烦。返回 `int`：0 成功、非 0 失败。

它还有几个变体：

- `omp_target_memcpy_async`：异步版本，挂在 OpenMP task 上执行。
- `omp_target_memcpy_rect`：多维矩形子数组的拷贝（递归降维）。

#### 4.3.2 核心流程

`omp_target_memcpy` 的核心是一个**四向 if-else 分发**，根据源/目标是否为主机选择不同路径：

```
omp_target_memcpy(Dst, Src, Length, DstOff, SrcOff, DstDev, SrcDev)
  ├─ 参数校验（Dst/Src 非空、Length>0，否则 OFFLOAD_FAIL）
  ├─ 计算带偏移的真实地址: DstAddr = Dst+DstOff, SrcAddr = Src+SrcOff
  │
  ├─ [1] 主机→主机 (src==初始 && dst==初始):
  │      memcpy(DstAddr, SrcAddr, Length)              ← 纯 libc
  │
  ├─ [2] 主机→设备 (src==初始):
  │      DstDevice.submitData(DstAddr, SrcAddr, Length) ← H2D
  │
  ├─ [3] 设备→主机 (dst==初始):
  │      SrcDevice.retrieveData(DstAddr, SrcAddr, Length) ← D2H
  │
  └─ [4] 设备→设备 (都不是主机):
         若 isDataExchangable → SrcDevice.dataExchange(...)  ← D2D 直传（快）
            成功则返回
         否则 fallback: 先 retrieveData 到主机中转 buffer，
                        再 submitData 到目标设备            ← D2D 经主机中转（慢）
```

第 [4] 条 D2D 路径值得细看：运行时**优先尝试**设备间直传（`dataExchange`，很多 GPU 支持 P2P 拷贝），只有当插件不支持或直传失败时，才退回到「设备→主机→设备」的中转模式。中转模式会 `malloc` 一段主机 buffer 当跳板，最后 `free` 掉。

异步变体 `omp_target_memcpy_async` 本身不直接搬运，而是把参数打包成 `TargetMemcpyArgsTy`，交给一个 **helper task**（`libomp_target_memcpy_async_task`），由 libomp 的任务调度器在合适的时机（满足 `depend` 依赖后）调用同步版的 `omp_target_memcpy`。

#### 4.3.3 源码精读

**四向分发的完整实现**（[API.cpp:284-367](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L284-L367)）。先看参数校验与偏移计算：

```cpp
EXTERN int omp_target_memcpy(void *Dst, const void *Src, size_t Length,
                             size_t DstOffset, size_t SrcOffset,
                             int DstDevice, int SrcDevice) {
  if (!Dst || !Src || Length <= 0) {
    if (Length == 0) return OFFLOAD_SUCCESS;   // 0 长度算成功
    REPORT() << "Call to " << __func__ << " with invalid arguments";
    return OFFLOAD_FAIL;
  }

  int Rc = OFFLOAD_SUCCESS;
  void *SrcAddr = (char *)const_cast<void *>(Src) + SrcOffset;   // 带偏移地址
  void *DstAddr = (char *)Dst + DstOffset;
  ...
```

**[1] 主机→主机**（[API.cpp:311-315](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L311-L315)），完全不走插件：

```cpp
if (isInitialDevice(SrcDevice) && isInitialDevice(DstDevice)) {
  const void *P = memcpy(DstAddr, SrcAddr, Length);
  if (P == NULL) Rc = OFFLOAD_FAIL;
}
```

**[2] 主机→设备** 与 **[3] 设备→主机**（[API.cpp:316-331](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L316-L331)），二者对称，都构造一个栈上的 `AsyncInfoTy`（u2-l7 讲过的异步上下文，这里用阻塞语义）：

```cpp
else if (isInitialDevice(SrcDevice)) {            // H2D
  auto DstDeviceOrErr = PM->getDevice(DstDevice);
  AsyncInfoTy AsyncInfo(*DstDeviceOrErr);
  Rc = DstDeviceOrErr->submitData(DstAddr, SrcAddr, Length, AsyncInfo);
}
else if (isInitialDevice(DstDevice)) {            // D2H
  auto SrcDeviceOrErr = PM->getDevice(SrcDevice);
  AsyncInfoTy AsyncInfo(*SrcDeviceOrErr);
  Rc = SrcDeviceOrErr->retrieveData(DstAddr, SrcAddr, Length, AsyncInfo);
}
```

注意：虽然传了 `AsyncInfoTy`，但因为它是栈上局部变量，函数返回时 `AsyncInfoTy` 的 RAII 析构会触发 `synchronize()`（u2-l7），所以对调用者而言**整体是阻塞的**。

**[4] 设备→设备**（[API.cpp:332-363](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L332-L363)），优先直传、失败回退中转：

```cpp
else {
  auto SrcDeviceOrErr = PM->getDevice(SrcDevice);
  auto DstDeviceOrErr = PM->getDevice(DstDevice);
  // 先试 D2D 直传（更高效）
  if (SrcDeviceOrErr->isDataExchangable(*DstDeviceOrErr)) {
    AsyncInfoTy AsyncInfo(*SrcDeviceOrErr);
    Rc = SrcDeviceOrErr->dataExchange(SrcAddr, *DstDeviceOrErr, DstAddr,
                                      Length, AsyncInfo);
    if (Rc == OFFLOAD_SUCCESS) return OFFLOAD_SUCCESS;   // 直传成功，直接返回
  }
  // 回退：经主机 buffer 中转
  void *Buffer = malloc(Length);
  { AsyncInfoTy AsyncInfo(*SrcDeviceOrErr);
    Rc = SrcDeviceOrErr->retrieveData(Buffer, SrcAddr, Length, AsyncInfo); } // D2H
  if (Rc == OFFLOAD_SUCCESS) {
    AsyncInfoTy AsyncInfo(*DstDeviceOrErr);
    Rc = DstDeviceOrErr->submitData(DstAddr, Buffer, Length, AsyncInfo); }   // H2D
  free(Buffer);
}
```

这段是「性能优化 + 鲁棒回退」的经典写法：能用 P2P 就用 P2P，不能用就用两跳中转兜底。

**异步版的 helper task 机制**（[API.cpp:527-554](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L527-L554) 与 [API.cpp:428-461](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L428-L461)）。`omp_target_memcpy_async` 把参数 `new` 成 `TargetMemcpyArgsTy`，然后调用 `libomp_helper_task_creation` 创建一个**隐藏 helper task**：

```cpp
EXTERN int omp_target_memcpy_async(void *Dst, const void *Src, ...) {
  if (Dst == nullptr || Src == nullptr) return OFFLOAD_FAIL;
  TargetMemcpyArgsTy *Args = new TargetMemcpyArgsTy(...);   // 堆上打包参数
  int Rc = libomp_helper_task_creation(Args, &libomp_target_memcpy_async_task,
                                       DepObjCount, DepObjList);
  return Rc;
}
```

`libomp_helper_task_creation`（[API.cpp:428-461](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L428-L461)）通过 `__kmpc_omp_target_task_alloc` + `__kmpc_omp_task_with_deps` 把搬运注册为带依赖的 OpenMP task，task 体 `libomp_target_memcpy_async_task`（[API.cpp:370-400](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L370-L400)）在调度执行时才真正调用同步版 `omp_target_memcpy`，并 `delete` 掉参数对象。这就是为什么异步版能和 `depend` 子句协作——它本质是个 OpenMP task。

#### 4.3.4 代码实践

**实践目标**：用 `omp_target_memcpy` 在两台设备间拷贝数据，并理解源码里的设备选择。

**操作步骤**：

1. 写程序（参考真实 D2D 测试 [test/offloading/d2d_memcpy.c](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/test/offloading/d2d_memcpy.c)）。如果只有一台设备，就做 H2D/D2H：

   ```c
   // 示例代码：单设备下的 H2D + D2H 搬运
   #include <stdio.h>
   #include <omp.h>

   int main(void) {
       const int N = 128;
       const int dev = 0;
       const size_t len = N * sizeof(int);

       if (omp_get_num_devices() == 0) { printf("PASS\n"); return 0; }

       int *dp = (int *)omp_target_alloc(len, dev);          // 设备内存
       int  host_in[N], host_out[N];
       for (int i = 0; i < N; ++i) host_in[i] = i;

       // H2D：主机 → 设备 0
       omp_target_memcpy(dp, host_in, len, 0, 0, dev, omp_get_initial_device());

       // D2H：设备 0 → 主机
       omp_target_memcpy(host_out, dp, len, 0, 0, omp_get_initial_device(), dev);

       int ok = 1;
       for (int i = 0; i < N; ++i) if (host_out[i] != i) ok = 0;
       printf("%s\n", ok ? "PASS" : "FAIL");

       omp_target_free(dp, dev);
       return 0;
   }
   ```

2. 对照源码，说明「运行时如何选择设备与搬运方向」：
   - 调用 H2D 时 `SrcDevice = omp_get_initial_device()`，`isInitialDevice(SrcDevice)` 为真且 `DstDevice = dev` 非主机 → 进入 [2] 分支，`PM->getDevice(dev).submitData(...)`。
   - 调用 D2H 时 `DstDevice = omp_get_initial_device()`，`isInitialDevice(DstDevice)` 为真 → 进入 [3] 分支，`PM->getDevice(dev).retrieveData(...)`。

3. 编译运行：

   ```bash
   clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu memcpy_demo.c -o memcpy_demo
   ./memcpy_demo
   ```

**需要观察的现象**：两次 `omp_target_memcpy` 分别命中 `submitData`（H2D）和 `retrieveData`（D2H）分支。开启 `LIBOMPTARGET_INFO=4`（数据传输位）能看到对应搬运日志。

**预期结果**：输出 `PASS`。**待本地验证**具体 INFO 输出。

#### 4.3.5 小练习与答案

**练习 1**：当 `SrcDevice` 和 `DstDevice` 都是非主机设备、且两台设备不支持 P2P 直传时，`omp_target_memcpy` 会怎么完成拷贝？

**参考答案**：`isDataExangable` 返回 false（或 `dataExchange` 返回失败），运行时回退到中转模式：先 `malloc` 一段主机 buffer，用 `retrieveData` 把数据从源设备搬到 buffer（D2H），再用 `submitData` 把 buffer 搬到目标设备（H2D），最后 `free(buffer)`。代价是两次跨 PCIe/NVLink 的搬运加一次主机中转。

**练习 2**：为什么 `omp_target_memcpy_async` 不直接调用插件的非阻塞拷贝，而是创建一个 OpenMP helper task？

**参考答案**：为了和 OpenMP 的 `depend` 依赖机制协作。把搬运包成 task 后，可以用 `DepObjList` 声明它依赖哪些先前的操作（如某个 kernel 完成），由 libomp 调度器在依赖满足后才执行搬运。这样用户能用统一的 `taskwait` / `depend` 模型编排「kernel → 搬运 → kernel」的流水线，而不必手动管理异步事件。

---

### 4.4 映射查询：is_present / is_accessible / associate_ptr / get_mapped_ptr

#### 4.4.1 概念说明

这一组 API 用来**查询或操作 u2-l4 讲的主机-设备映射表**（`HDTTMap`）。它们不分配新内存，只读取或建立「主机指针 ↔ 设备指针」的关联记录：

| API | 作用 |
|-----|------|
| `omp_target_is_present(ptr, dev)` | 查询主机指针 `ptr` 是否已在设备 `dev` 上有映射 |
| `omp_target_is_accessible(ptr, size, dev)` | 查询主机指针 `ptr` 指向的 `size` 字节能否被设备 `dev` 直接访问（如 unified memory） |
| `omp_target_associate_ptr(hptr, dptr, size, off, dev)` | **手动**把一段已存在的设备指针 `dptr` 关联到主机指针 `hptr` |
| `omp_target_disassociate_ptr(hptr, dev)` | 解除上述手动关联 |
| `omp_get_mapped_ptr(ptr, dev)` | 查询主机指针 `ptr` 在设备 `dev` 上对应的设备指针 |

`associate_ptr` 是个高级用法：当用户已经通过其他途径（比如直接调 CUDA `cudaMalloc`）拿到了设备指针，可以用它把这个设备指针「登记」进 libomptarget 的映射表，这样后续的 `map` 子句就能复用这块内存而不重复分配。

#### 4.4.2 核心流程

四个查询函数都有相同的「主机短路 + 设备查询」骨架：

```
omp_target_is_present(Ptr, DeviceNum):
  ├─ if Ptr == NULL      → false
  ├─ if isInitialDevice  → true（主机上一切"都在"）
  └─ Device.getMappingInfo().getTgtPtrBegin(Ptr, 1,
                           UpdateRefCount=false, UseHoldRefCount=false)
       └─ 返回 TPR.isPresent()
```

```
omp_target_is_accessible(Ptr, Size, DeviceNum):
  ├─ if Ptr == NULL      → false
  ├─ if isInitialDevice  → true
  └─ Device.isAccessiblePtr(Ptr, Size)
       └─ DeviceTy → RTL->is_accessible_ptr(RTLDeviceID, Ptr, Size)
```

`is_present` 与 `is_accessible` 的关键区别：

- `is_present` 查**映射表**——「这个主机指针是否曾被 `map` 进设备」。它用 u2-l4 的 `getTgtPtrBegin` 查表，且明确传 `UpdateRefCount=false`（只查不改引用计数）。
- `is_accessible` 查**硬件能力**——「这段主机内存能否被设备直接寻址」（如 unified/shared memory、APU 上主机与设备共享内存）。它直接问插件 `is_accessible_ptr`，不看映射表。

`get_mapped_ptr` 也用 `getTgtPtrBegin` 查表，但返回的是设备指针本身（而非布尔值）；查不到返回 `NULL`。

`associate_ptr` / `disassociate_ptr` 则调用 `MappingInfoTy::associatePtr` / `disassociatePtr`，在映射表里**新增/删除**一条引用计数为无穷大（INF）的记录。

#### 4.4.3 源码精读

**`omp_target_is_present`**（[API.cpp:222-254](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L222-L254)）：

```cpp
EXTERN int omp_target_is_present(const void *Ptr, int DeviceNum) {
  if (!Ptr) return false;
  if (isInitialDevice(DeviceNum)) return true;       // 主机上恒"存在"

  auto DeviceOrErr = PM->getDevice(DeviceNum);
  ...
  // 注意：只查 1 字节（因为没有 size 参数），且不改引用计数
  TargetPointerResultTy TPR =
      DeviceOrErr->getMappingInfo().getTgtPtrBegin(const_cast<void *>(Ptr), 1,
                                                   /*UpdateRefCount=*/false,
                                                   /*UseHoldRefCount=*/false);
  return TPR.isPresent();
}
```

源码注释点出一个微妙之处：`is_present` 没有 `size` 参数，所以只能查「指针所指的那 1 字节」是否已映射（不能传 0，0 会被当成零长数组查询，语义不同）。

**`omp_target_is_accessible`**（[API.cpp:258-282](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L258-L282)），它带 `size` 且走插件能力查询：

```cpp
EXTERN int omp_target_is_accessible(const void *Ptr, size_t Size, int DeviceNum) {
  if (!Ptr) return false;
  if (isInitialDevice(DeviceNum)) return true;

  auto DeviceOrErr = PM->getDevice(DeviceNum);
  ...
  return DeviceOrErr->isAccessiblePtr(Ptr, Size);    // 不查映射表，查硬件
}
```

`DeviceTy::isAccessiblePtr`（[libomptarget/device.cpp:428-430](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L428-L430)）依旧是一行转发：

```cpp
bool DeviceTy::isAccessiblePtr(const void *Ptr, size_t Size) {
  return RTL->is_accessible_ptr(RTLDeviceID, Ptr, Size);
}
```

**`omp_target_associate_ptr`**（[API.cpp:659-694](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L659-L694)），把用户已持有的设备指针登记进映射表：

```cpp
EXTERN int omp_target_associate_ptr(const void *HostPtr, const void *DevicePtr,
                                    size_t Size, size_t DeviceOffset,
                                    int DeviceNum) {
  if (!HostPtr || !DevicePtr || Size <= 0) return OFFLOAD_FAIL;
  if (isInitialDevice(DeviceNum)) return OFFLOAD_FAIL;   // 主机上不能关联

  auto DeviceOrErr = PM->getDevice(DeviceNum);
  ...
  void *DeviceAddr = (void *)((uint64_t)DevicePtr + (uint64_t)DeviceOffset);

  int Rc = DeviceOrErr->getMappingInfo().associatePtr(
      const_cast<void *>(HostPtr), const_cast<void *>(DeviceAddr), Size);
  return Rc;
}
```

`DeviceOffset` 会让设备地址偏移——用户传入的 `DevicePtr` 可能是个池的基址，真实数据在偏移 `DeviceOffset` 处。最终调用 `MappingInfoTy::associatePtr`（u2-l4），它会建一条引用计数为 INF（永不释放）的映射记录。

**`omp_get_mapped_ptr`**（[API.cpp:727-768](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/API.cpp#L727-L768)），查映射表取设备指针：

```cpp
EXTERN void *omp_get_mapped_ptr(const void *Ptr, int DeviceNum) {
  if (!Ptr) { REPORT(...) << "with nullptr."; return nullptr; }

  if (isInitialDevice(DeviceNum)) return const_cast<void *>(Ptr);  // 主机→主机，原样返回
  if (NumDevices <= DeviceNum) return nullptr;                     // 非法设备号

  auto DeviceOrErr = PM->getDevice(DeviceNum);
  ...
  TargetPointerResultTy TPR =
      DeviceOrErr->getMappingInfo().getTgtPtrBegin(const_cast<void *>(Ptr), 1,
                                                   /*UpdateRefCount=*/false, ...);
  if (!TPR.isPresent()) return nullptr;            // 未映射 → NULL
  return TPR.TargetPointer;                        // 返回设备指针
}
```

注意主机设备时的特殊处理：在主机上「设备指针」就是主机指针本身，所以原样返回。

#### 4.4.4 代码实践

**实践目标**：通过 `omp_target_is_present` 观察 `map` 子句如何改变映射表，区分 `is_present`（查表）与 `is_accessible`（查能力）。

**操作步骤**：

1. 写程序 `probe_mapping.c`：

   ```c
   // 示例代码
   #include <stdio.h>
   #include <omp.h>

   int main(void) {
       int x = 42;
       int dev = 0;

       if (omp_get_num_devices() == 0) { printf("PASS\n"); return 0; }

       // 进入 target data 前，x 在设备 0 上"不存在"
       printf("before: is_present=%d\n", omp_target_is_present(&x, dev));

   #pragma omp target data map(to: x) device(dev)
       {
           // 进入 data 区域后，x 已被映射到设备 0
           printf("inside: is_present=%d\n", omp_target_is_present(&x, dev));
           void *dp = omp_get_mapped_ptr(&x, dev);
           printf("inside: mapped_ptr=%p (host &x=%p)\n", dp, (void *)&x);
       }

       // 退出后引用计数归零，映射被回收
       printf("after:  is_present=%d\n", omp_target_is_present(&x, dev));
       printf("PASS\n");
       return 0;
   }
   ```

2. 编译运行：

   ```bash
   clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu probe_mapping.c -o probe_mapping
   ./probe_mapping
   ```

3. 对照源码解释输出：
   - `before` 为 0：`x` 从未被 `map`，`getTgtPtrBegin` 查不到记录，`TPR.isPresent()` 为假。
   - `inside` 为 1：进入 `target data map(to:)` 后，u2-l5 的 `targetDataBegin` 调 `getTargetPointer` 建立了映射，`getTgtPtrBegin` 命中。
   - `after` 为 0：退出 `target data` 时 `targetDataEnd` 把引用计数减到 0，映射记录被删除（u2-l4 的延迟删除）。

**需要观察的现象**：`is_present` 的三个值分别应为 0、1、0，印证「映射表随 data 区域进入/退出而增删」。`mapped_ptr` 在主机设备外应是一个与 `&x` 不同的设备地址。

**预期结果**：在 host 插件上 `mapped_ptr` 可能等于 `&x`（host 插件把主机内存当设备内存），但 `is_present` 的 0/1/0 模式应成立。**待本地验证**具体指针值。

#### 4.4.5 小练习与答案

**练习 1**：`omp_target_is_present` 和 `omp_target_is_accessible` 都返回 `int` 当布尔值，但内部查询路径完全不同。请说明区别。

**参考答案**：`is_present` 查 libomptarget 自己维护的映射表 `HDTTMap`（用 `getTgtPtrBegin`），回答「这个主机指针是否曾被 `map` 进设备」；`is_accessible` 完全不查映射表，而是调插件的 `is_accessible_ptr`，回答「这段主机内存能否被设备硬件直接寻址」（典型场景是 unified memory 或 APU 共享内存，此时即使没有 `map` 也能直接访问）。

**练习 2**：为什么 `omp_target_associate_ptr` 在主机设备上直接返回 `OFFLOAD_FAIL`？

**参考答案**：关联的本质是「把一个设备指针登记进主机-设备映射表」。主机设备上，主机指针本身就是设备指针，不存在「另一块设备内存」可以关联，映射表概念也不适用，所以规范和实现都禁止在主机上调用它。

**练习 3**：`omp_get_mapped_ptr` 在主机设备上调用时返回什么？为什么？

**参考答案**：返回传入的 `Ptr` 本身。因为在主机设备上，「设备指针」就是主机指针，二者相同，源码里直接 `return const_cast<void *>(Ptr)` 走短路。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个小型「显式内存搬运器」：

**任务**：写一个程序，完成以下流程，并对照源码解释每一步运行时的选择：

1. 用 `omp_get_num_devices()` / `omp_get_initial_device()` 打印设备数与主机号。
2. 用 `omp_get_uid_from_device` 打印设备 0 的 UID，再用 `omp_get_device_from_uid` 验证往返一致。
3. 在设备 0 上用 `omp_target_alloc` 分配一段内存，用 `omp_target_is_present` 验证它**不在**映射表里（因为是显式分配）。
4. 用 `omp_target_memcpy` 把一段主机数据搬到这块设备内存（H2D），再搬回（D2H）验证内容一致。
5. 用 `llvm_omp_target_alloc_shared` 再分配一块共享内存，对比它与普通 device 分配在 `is_accessible` 上的差异（如果后端支持）。
6. 用 `omp_target_free` / `llvm_omp_target_free_shared` 成对释放，注意 Kind 匹配。

**验收要点**：

- 能正确解释第 3 步「显式分配不在映射表」与第 4 步「memcpy 走 submitData/retrieveData 分支」的源码依据。
- 能指出第 6 步如果用错 Kind 释放，`targetFreeExplicit` 会 `FATAL_MESSAGE` 中止。
- 全程开启 `LIBOMPTARGET_INFO=63`，把日志与源码分支一一对应。

如果本机没有 GPU，用 host 插件（`-fopenmp-targets=x86_64-pc-linux-gnu`）即可完成全部步骤；host 插件下 `is_accessible` 与 device/shared 的差异不明显（都退化为 `malloc`），这部分**待本地验证**（在有 unified memory 的 APU 或 NVIDIA GPU 上差异才显著）。

---

## 6. 本讲小结

- `omp_target_*` 是 OpenMP 用户面 API，与编译器自动生成的 `__tgt_*` 隐式链路并列，让用户能**手动**管理设备内存与搬运，分配出的指针**不进入**映射表。
- 设备有两套标识：**DeviceNum**（连续下标，方便但不稳定）和 **UID**（字符串 `<plugin>-<vendor_uid>`，稳定）。主机在两套体系里都是特例：DeviceNum = `N` 或 `-1`，UID = `"HOST"`。
- 所有用户面 API 都有「**主机短路 + 设备转发**」的统一骨架：`isInitialDevice(DeviceNum)` 为真就走主机快速路径（`malloc`/`memcpy`），否则经 `PM->getDevice()` 取 `DeviceTy`，再由 `DeviceTy` Facade 一行转发给插件。
- 显式分配器用 `TargetAllocTy` 枚举（`DEVICE`/`HOST`/`SHARED`/`DEFAULT`）区分内存空间，Kind 从用户 API 一路透传到插件的 `data_alloc`/`data_delete`；**分配与释放的 Kind 必须匹配**，否则释放失败会 fail-fast 中止。
- `omp_target_memcpy` 按「主机↔主机 / 主机↔设备 / 设备↔主机 / 设备↔设备」四向分发，D2D 优先尝试 P2P 直传（`dataExchange`）、失败回退经主机中转；异步版通过 helper task 与 `depend` 协作。
- `is_present` 查映射表、`is_accessible` 查硬件能力、`associate_ptr` 手动登记映射、`get_mapped_ptr` 取设备指针——四个查询 API 对应 u2-l4 映射机制的不同侧面。

---

## 7. 下一步学习建议

本讲讲完的是 libomptarget 的**用户面 C API**，它们最终都汇聚到 `DeviceTy` Facade 再转发给插件。接下来建议：

- **横向**：阅读 u3-l1（通用插件接口 `GenericPluginTy`/`GenericDeviceTy`），看 `DeviceTy::allocData` 转发下去的 `RTL->data_alloc` 在插件框架里如何被 `dataAlloc`/`allocate` 实现，理解 `TargetAllocTy` 在 GPU 后端如何映射到真实内存池。
- **纵向（内存管理）**：阅读 u3-l6（设备内存管理器），看 `MemoryManagerTy` 的 free-list 策略与 OOM 时「释放 free-list 重试」的回收路径，理解 `allocData` 失败时的兜底机制。
- **实践**：尝试在 u3-l3（host 插件走读）的基础上，对照本讲的 `omp_target_alloc`，跟踪一次完整调用从 API.cpp → omptarget.cpp → device.cpp → 插件 `allocate` 的全链路，画出调用栈。
- **延伸**：阅读 liboffload（u3-l11）的统一对象模型，对比 `olMemAlloc` 与 `omp_target_alloc` 在设计哲学上的差异（前者不绑定 OpenMP）。
