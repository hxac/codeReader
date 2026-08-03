# 运行时初始化与库注册入口

## 1. 本讲目标

本讲是「核心运行时调用链」的第一讲，承接 [u1-l5](u1-l5-compiler-runtime-contract.md) 建立的编译器-运行时契约。学完本讲你应该能够：

- 说清 `__tgt_rtl_init` / `__tgt_rtl_deinit`、`__tgt_register_lib` / `__tgt_unregister_lib`、`__tgt_init_all_rtls` 这几条入口的职责与调用顺序。
- 描述运行时如何用「引用计数 + `RTLAlive`」管理整个 `PluginManager` 的生命周期。
- 复述 `__tgt_register_lib` 从一个二进制描述符 `__tgt_bin_desc` 出发，最终把设备镜像交给「兼容插件 + 兼容设备」并建立翻译表的完整路径。
- 理解「延迟注册（delayed registration）」要解决什么问题，以及 `requires` 标志为何不再由 `__tgt_register_requires` 传入。

本讲只聚焦**入口与初始化**，不展开数据搬运与内核启动（那是后续讲义的内容）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，运行时是「被驱动」的，不是自己跑起来的。** Clang 在生成主机代码时，会在含卸载区域的翻译单元里插入两段：一段是「二进制描述符」`__tgt_bin_desc`（记录所有设备镜像与主机符号表），另一段是对运行时入口函数（如 `__tgt_register_lib`）的调用。换句话说，主机程序一启动，就会主动「告诉」`libomptarget.so`：我这里有一份设备镜像，请帮我管起来。运行时自己不会去扫描内存找镜像。

**第二，运行时是全局单例 + 引用计数的。** 整个进程里只有一个 `PluginManager *PM`。它可能被多次「进入」和「退出」（多个库各自注册/注销），所以用一个 `RefCount` 来决定何时真正构造、何时真正销毁。

**第三，运行时只「分发」，不「翻译」。** 镜像在编译期就已经是目标设备能执行的格式（如 PTX、GCN、SPIR-V、或主机 ELF）。运行时的工作是：挑出哪个插件能识别这个镜像 → 把镜像加载到对应设备 → 建立一张「主机符号 ↔ 设备符号」的翻译表，供后续 `target` 区域查找。这一点在 [u1-l5](u1-l5-compiler-runtime-contract.md) 已强调，本讲你会看到它在源码里具体长什么样。

涉及的几个关键名词：`__tgt_bin_desc`（二进制描述符，详见 [u1-l5](u1-l5-compiler-runtime-contract.md)）、插件（`GenericPluginTy`）、设备（`DeviceTy`）、翻译表（`TranslationTable`）、`requires` 标志（用户对运行时特性的强制要求，如统一共享内存）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/omptarget.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h) | 契约层。声明 `__tgt_rtl_init` / `__tgt_register_lib` 等所有入口的 C ABI 签名。 |
| [libomptarget/interface.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp) | 入口实现层。把编译器发来的 `__tgt_*` 调用转发给 `PluginManager`。 |
| [libomptarget/OffloadRTL.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OffloadRTL.cpp) | 运行时生命周期层。定义 `initRuntime` / `deinitRuntime`，管理引用计数与单例 `PM`。 |
| [include/PluginManager.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h) | `PluginManager` 结构体声明：设备容器、插件列表、`registerLib` / `delayRegisterLib` 等。 |
| [libomptarget/PluginManager.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp) | `PluginManager` 的核心实现：插件/设备初始化、镜像-插件匹配、翻译表构建、镜像加载。 |
| [include/Shared/Requirements.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h) | `requires` 标志枚举与 `RequirementCollection` 的一致性检查。 |

---

## 4. 核心概念与源码讲解

### 4.1 运行时生命周期入口：`__tgt_rtl_init` / `__tgt_rtl_deinit`

#### 4.1.1 概念说明

运行时并非在进程启动时就自动初始化完毕，而是**懒初始化**：第一次有人需要它时才真正构造。`__tgt_rtl_init` 和 `__tgt_rtl_deinit` 是暴露给外部的「初始化 / 去初始化」入口；它们各自只是对内部 `initRuntime()` / `deinitRuntime()` 的一行转发：

[libomptarget/interface.cpp:86-87](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L86-L87) —— `__tgt_rtl_init` 调 `initRuntime()`，`__tgt_rtl_deinit` 调 `deinitRuntime()`。

声明在 [include/omptarget.h:331-334](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L331-L334)。

真正干活的是 `initRuntime`，定义在 OffloadRTL.cpp：

#### 4.1.2 核心流程

`initRuntime` 用一个互斥锁 `PluginMtx` 和一个 `RefCount` 实现「首次进入才构造、末次退出才销毁」：

```text
initRuntime():
  加锁 PluginMtx
  若 PM == nullptr:  PM = new PluginManager()   // 单例
  RefCount++
  若 RefCount == 1:                          // 首次进入
      (若开启 OMPT) 先 connectLibrary() 初始化 OMPT
      PM->init()                             // 创建所有插件实例
      PM->registerDelayedLibraries()         // 重放被暂存的二进制描述符
      RTLAlive = true                        // 标记运行时已就绪
```

关键点：`PM` 是全局唯一的 `PluginManager *PM`，初值为 `nullptr`（[libomptarget/PluginManager.cpp:27](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L27)）。`RefCount` 决定真正的构造/销毁时机，避免重复初始化。

`deinitRuntime` 是镜像对称的逻辑，但多了一步**等待进行中的同步**：先把 `RTLAlive` 置 `false`，再忙等 `RTLOngoingSyncs` 归零，最后才 `PM->deinit()` 并 `delete PM`。

#### 4.1.3 源码精读

`initRuntime` 全貌（[libomptarget/OffloadRTL.cpp:38-62](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OffloadRTL.cpp#L38-L62)）：

```cpp
void initRuntime() {
  std::scoped_lock<decltype(PluginMtx)> Lock(PluginMtx);
  Profiler::get();
  TIMESCOPE();
  checkRuntimeEnvironment();
  if (PM == nullptr)
    PM = new PluginManager();
  RefCount++;
  if (RefCount == 1) {
    ODBG(ODT_Init) << "Init offload library!";
#ifdef OMPT_SUPPORT
    llvm::omp::target::ompt::connectLibrary();   // OMPT 必须最先初始化
#endif
    PM->init();
    PM->registerDelayedLibraries();
    RTLAlive = true;
  }
}
```

`RTLAlive` 与 `RTLOngoingSyncs` 是两个全局原子量（[libomptarget/OffloadRTL.cpp:26-27](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OffloadRTL.cpp#L26-L27)）。`RTLAlive` 标记运行时是否存活，`RTLOngoingSyncs` 记录当前有多少个外部同步正在进行——去初始化时必须等它们全部结束，否则可能在插件已经卸载后还有线程在访问 native 库。

`deinitRuntime` 的「等同步归零」逻辑（[libomptarget/OffloadRTL.cpp:64-83](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OffloadRTL.cpp#L64-L83)）：

```cpp
if (RefCount == 1) {
  RTLAlive = false;
  while (RTLOngoingSyncs > 0) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  PM->deinit();
  delete PM;
  PM = nullptr;
}
RefCount--;
```

#### 4.1.4 代码实践

**目标**：观察「首次进入才初始化」的效果。

**步骤**：

1. 在 [OffloadRTL.cpp:50](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OffloadRTL.cpp#L50) 的 `ODBG(ODT_Init) << "Init offload library!";` 旁边，理解这条日志只在 `RefCount == 1` 时打印一次。
2. 用 Debug 构建（或在开启 `LIBOMPTARGET_DEBUG` 的构建，见 [u1-l4](u1-l4-toolchain-and-run.md)）运行一个最小的卸载程序。
3. **待本地验证**：观察 `Init offload library!` 是否在整个进程生命周期里只出现一次，即使程序里有多个 `target` 区域、多个 `__tgt_register_lib` 调用。

#### 4.1.5 小练习与答案

**练习 1**：如果两个线程同时第一次调用 `initRuntime()`，会发生什么？
**答案**：`std::scoped_lock` 保证只有一个线程进入临界区；该线程 `RefCount` 从 0 变 1，执行 `PM->init()` 等构造；另一个线程被阻塞，进入后 `RefCount` 从 1 变 2，跳过构造分支。`PM` 只被 `new` 一次。

**练习 2**：为什么 `deinitRuntime` 里要先 `RTLAlive = false` 再等 `RTLOngoingSyncs` 归零，而不是直接 `delete PM`？
**答案**：此时可能有线程正在插件里做设备同步（持有 native 运行时的句柄）。先置 `RTLAlive = false` 让其他路径知道运行时正在关闭，再等所有进行中的同步结束，才能安全地 `PM->deinit()` 卸载 native 库并销毁 `PM`，避免 use-after-free。

---

### 4.2 库注册主入口：`__tgt_register_lib` 与延迟注册

#### 4.2.1 概念说明

`__tgt_register_lib` 是**最核心的注册入口**。主机程序启动时，Clang 生成的构造器会带着一份 `__tgt_bin_desc *Desc` 调用它，把「我有哪些设备镜像、有哪些主机符号」登记给运行时。

它只有三步（[libomptarget/interface.cpp:91-97](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L91-L97)）：

```cpp
EXTERN void __tgt_register_lib(__tgt_bin_desc *Desc) {
  initRuntime();                      // 1. 确保运行时已初始化
  if (PM->delayRegisterLib(Desc))     // 2. 若插件尚未就绪，先暂存
    return;
  PM->registerLib(Desc);              // 3. 立即注册
}
```

注意第一步：**注册前必然先初始化运行时**。这是「懒初始化」的关键触发点——很多程序根本不显式调 `__tgt_rtl_init`，而是靠第一次 `__tgt_register_lib` 把运行时拉起来。

#### 4.2.2 核心流程：延迟注册要解决什么

`delayRegisterLib` 解决的是一个**初始化顺序竞态**：某些插件在自身初始化时会 `dlopen` 一个共享库，而该库的构造器又会反过来调用 `__tgt_register_lib`。此时插件列表可能还没完全建立，贸然注册会出问题。解决办法是「先暂存，后重放」。

[include/PluginManager.h:96-112](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L96-L112) 给出了这对方法（代码注释直接解释了 dlopen 场景）：

```text
delayRegisterLib(Desc):
  if (RTLsLoaded == false):     // 插件尚未全部就绪
      DelayedBinDesc.push_back(Desc)   // 暂存
      return true                       // 告诉调用方「我收下了，别再处理」
  return false                          // 插件已就绪，调用方应立即注册

registerDelayedLibraries():     // 在 initRuntime 末尾被调用
  RTLsLoaded = true
  for Desc in DelayedBinDesc:
      __tgt_register_lib(Desc)  // 重放：此时 RTLsLoaded 已 true，会走 registerLib
  DelayedBinDesc.clear()
```

正常路径下，`__tgt_register_lib` 先 `initRuntime()`（此时 `RTLsLoaded` 被置 true），所以 `delayRegisterLib` 返回 false，直接进入 `registerLib`。延迟路径只在「运行时尚未完成初始化时就有库想注册」的边角场景下触发。

#### 4.2.3 源码精读

`delayRegisterLib` 与 `registerDelayedLibraries`（[include/PluginManager.h:99-112](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L99-L112)）：

```cpp
bool delayRegisterLib(__tgt_bin_desc *Desc) {
  if (RTLsLoaded)
    return false;
  DelayedBinDesc.push_back(Desc);
  return true;
}

void registerDelayedLibraries() {
  // Only called by libomptarget constructor
  RTLsLoaded = true;
  for (auto *Desc : DelayedBinDesc)
    __tgt_register_lib(Desc);
  DelayedBinDesc.clear();
}
```

`RTLsLoaded` 与暂存区 `DelayedBinDesc` 是私有成员（[include/PluginManager.h:154-155](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L154-L155)）。`registerDelayedLibraries` 被 `initRuntime` 在 `PM->init()` 之后调用（见 4.1.3），保证重放时插件实例已创建。

注销入口 `__tgt_unregister_lib`（[libomptarget/interface.cpp:108-112](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L108-L112)）是对称的：先 `PM->unregisterLib(Desc)` 清理翻译表与符号映射，再 `deinitRuntime()` 把引用计数减一。

#### 4.2.4 代码实践

**目标**：验证「注册前必先初始化」与「延迟注册」两条路径。

**步骤**：

1. 阅读 [interface.cpp:91-97](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L91-L97)，确认 `__tgt_register_lib` 第一个动作总是 `initRuntime()`。
2. 在 `delayRegisterLib` 的 `DelayedBinDesc.push_back(Desc);` 一行旁，思考：什么条件下这一行会被执行？
3. **待本地验证**：在 Debug 构建下运行普通卸载程序，观察日志里是否出现 `RTLs loaded!`（来自 `PM->init()`）出现在任何 `Done registering entries!`（来自 `registerLib`）之前。

#### 4.2.5 小练习与答案

**练习 1**：若 `RTLsLoaded` 已经是 true，`delayRegisterLib(Desc)` 返回什么？调用方接下来做什么？
**答案**：返回 false。`__tgt_register_lib` 里的 `if (PM->delayRegisterLib(Desc)) return;` 不成立，继续执行 `PM->registerLib(Desc)` 立即注册。

**练习 2**：为什么 `registerDelayedLibraries` 要在 `PM->init()` 之后调用，而不是之前？
**答案**：`PM->init()` 负责创建所有插件实例（见 4.5）。重放 `DelayedBinDesc` 时会再次进入 `__tgt_register_lib` → `registerLib`，而 `registerLib` 需要遍历插件列表去匹配镜像。若插件还没创建，注册就无法进行。

---

### 4.3 镜像-插件匹配与翻译表构建：`registerLib` 核心流程

#### 4.3.1 概念说明

`PluginManager::registerLib` 是本讲的「重头戏」，它回答了一个核心问题：**给定一份二进制描述符，运行时如何决定把每个设备镜像交给哪个设备的哪个插件？**

回顾 `__tgt_bin_desc` 的结构（[include/Shared/APITypes.h:46-53](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L46-L53)）：它含 `NumDeviceImages` 个 `__tgt_device_image`（每个是一段设备镜像 + 一张符号表），以及一段主机符号表 `HostEntriesBegin .. HostEntriesEnd`。一份「胖二进制」里通常嵌着多种目标的镜像（例如同时有 PTX 和 GCN），运行时要为每个镜像找到能识别它的插件。

#### 4.3.2 核心流程

`registerLib` 大致分四步（[libomptarget/PluginManager.cpp:196-317](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L196-L317)）：

```text
registerLib(Desc):
  1. Desc = upgradeLegacyEntries(Desc)        // 兼容旧版条目格式
  2. 扫描主机条目，把 OMP_REGISTER_REQUIRES 的需求收进 Requirements
  3. for 每个 DeviceImage: PM->addDeviceImage(...)   // 抽取并保存镜像
  4. for 每个 DeviceImage (Img):
       for 每个插件 R:
         if !R.isPluginCompatible(Img):  continue    // 插件能否识别这段镜像？
         initializePlugin(R)                         // 懒初始化该插件
         for 该插件的每个设备 DeviceId:
           if !R.isDeviceCompatible(DeviceId, Img): continue  // 设备是否兼容？
           initializeDevice(R, DeviceId)             // 懒初始化该设备
           把 Img 登记进 TranslationTable[UserId]    // 建立翻译表条目
           记录 UsedImages / UsedDevices，避免重复登记
  5. 检查首设备是否触发 Auto Zero-Copy，按需追加 requirement
```

注意「双层兼容判断」：先 `isPluginCompatible`（这个插件家族认不认这段镜像的字节？例如 CUDA 插件认 PTX、AMDGPU 插件认 GCN），再 `isDeviceCompatible`（这个插件里的具体某块卡能不能跑？例如 sm_89 的镜像不能跑在 sm_70 的卡上）。这解释了为什么一个镜像可能匹配插件却不匹配该插件的所有设备。

还要注意：**这里只是「登记镜像到翻译表」，并没有真正把镜像加载到设备。** 真正的 `loadBinary` 发生在设备第一次被实际使用时（见 4.5 与 `getDevice` 的延迟加载）。这是一个「按需加载」的设计。

#### 4.3.3 源码精读

requires 收集（[libomptarget/PluginManager.cpp:203-207](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L203-L207)）——遍历主机条目，遇到 `OMP_REGISTER_REQUIRES` 标志就把它的 `Data` 当作 requires 位掩码加入：

```cpp
for (llvm::offloading::EntryTy &Entry :
     llvm::make_range(Desc->HostEntriesBegin, Desc->HostEntriesEnd))
  if (Entry.Kind == object::OffloadKind::OFK_OpenMP &&
      Entry.Flags == OMP_REGISTER_REQUIRES)
    PM->addRequirements(Entry.Data);
```

镜像抽取（[libomptarget/PluginManager.cpp:210-211](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L210-L211)）：

```cpp
for (int32_t i = 0; i < Desc->NumDeviceImages; ++i)
  PM->addDeviceImage(*Desc, Desc->DeviceImages[i]);
```

双层兼容判断 + 设备初始化（[libomptarget/PluginManager.cpp:223-260](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L223-L260)），关键片段：

```cpp
for (auto &R : plugins()) {
  StringRef Buffer(...);
  if (!R.isPluginCompatible(Buffer))    // 第一层：插件级兼容
    continue;
  if (!initializePlugin(R))             // 懒初始化插件
    continue;
  if (!R.number_of_devices()) { ... continue; }
  for (int32_t DeviceId = 0; DeviceId < R.number_of_devices(); ++DeviceId) {
    if (UsedDevices[&R].contains(DeviceId)) { ... continue; }  // 去重
    if (!R.isDeviceCompatible(DeviceId, Buffer))   // 第二层：设备级兼容
      continue;
    if (!initializeDevice(R, DeviceId))            // 懒初始化设备
      continue;
    ...
  }
}
```

翻译表构建（[libomptarget/PluginManager.cpp:262-295](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L262-L295)）——把镜像登记到 `TranslationTable` 里以 `UserId`（OpenMP 设备号）为下标的位置：

```cpp
auto UserId = PM->DeviceIds[std::make_pair(&R, DeviceId)];
if (TT.TargetsTable.size() < static_cast<size_t>(UserId + 1)) {
  TT.DeviceTables.resize(UserId + 1, {});
  TT.TargetsImages.resize(UserId + 1, nullptr);
  TT.TargetsEntries.resize(UserId + 1, {});
  TT.TargetsTable.resize(UserId + 1, nullptr);
}
TT.TargetsImages[UserId] = Img;   // 记住「这个 OpenMP 设备号对应这份镜像」
TT.TargetsTable[UserId] = nullptr; // 设备表先置空，等真正 loadBinary 时再填
```

最后的 Auto Zero-Copy 检查（[libomptarget/PluginManager.cpp:303-314](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L303-L314)）：若第一个设备（APU 上）建议自动零拷贝，就追加 `OMPX_REQ_AUTO_ZERO_COPY` 需求。

#### 4.3.4 代码实践（本讲主任务）

**目标**：跟踪 `__tgt_register_lib` 的执行路径，画出「从二进制描述符注册到所有兼容插件加载设备镜像」的流程图。

**步骤**：

1. 从 [interface.cpp:91-97](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L91-L97) 出发，记下 `__tgt_register_lib(Desc)` 的三步。
2. 进入 [PluginManager.cpp:196-317](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L196-L317)，逐段标注：升级条目 → 收集 requires → 抽取镜像 → 双层匹配 → 登记翻译表。
3. 注意：`registerLib` 只登记镜像，**不**调用 `loadBinary`。真正的镜像加载在 [PluginManager.cpp:390-551](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L390-L551) 的 `loadImagesOntoDevice`，它由 `getDevice` 在设备首次使用时触发（见 4.5.3）。
4. 画一张流程图，包含两个阶段：
   - **注册期**（`registerLib`）：镜像 → 匹配插件/设备 → 写翻译表（不加载）。
   - **首次使用期**（`getDevice` → `loadImagesOntoDevice`）：`Device.loadBinary(Img)` → 解析符号 → 填充 `TargetsTable[UserId]`。

**预期结果**：流程图应清楚体现「注册」与「加载」是分离的两个阶段，这正是运行时按需加载的关键。

#### 4.3.5 小练习与答案

**练习 1**：一个 `__tgt_bin_desc` 里同时含 PTX 和 GCN 两份镜像，系统装了 NVIDIA 和 AMD 两个插件。`registerLib` 会怎么做？
**答案**：对 PTX 镜像，只有 CUDA 插件 `isPluginCompatible` 返回 true，于是登记到 CUDA 插件的设备；对 GCN 镜像，只有 AMDGPU 插件兼容，登记到 AMDGPU 插件的设备。两份镜像各得其所，互不干扰。

**练习 2**：为什么 `registerLib` 在匹配成功后只把 `TT.TargetsImages[UserId] = Img`，却把 `TT.TargetsTable[UserId] = nullptr`？
**答案**：因为此刻镜像还没被加载到设备，设备侧的符号表还不存在。`TargetsTable` 要等 `loadImagesOntoDevice` 调用 `Device.loadBinary` 解析出设备符号后才会被填充（见 [PluginManager.cpp:477-478](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L477-L478)）。置空表示「镜像已分配，但尚未加载」。

**练习 3**：`UsedDevices[&R].contains(DeviceId)` 这个去重检查防止了什么？
**答案**：防止同一份二进制描述符里多个互相兼容的镜像（如 sm_80 与 sm_89）被重复登记到同一块卡上。一个设备只接受第一个兼容的镜像。

---

### 4.4 requires 标志机制：从废弃入口到注册期收集

#### 4.4.1 概念说明

OpenMP 的 `requires` 指令让用户对整个程序强制规定某些运行时特性，例如 `unified_shared_memory`（统一共享内存）、`reverse_offload`（反向卸载）、`dynamic_allocators`。这些需求必须在程序开始执行前就生效，且**所有翻译单元必须一致**。

历史上，Clang 曾通过 `__tgt_register_requires(int64_t Flags)` 把这些标志传给运行时。但在当前代码里，这个入口**已被废弃**，仅保留符号以兼容旧二进制：

[libomptarget/interface.cpp:80-84](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L80-L84)：

```cpp
EXTERN void __tgt_register_requires(int64_t Flags) {
  MESSAGE("The %s function has been removed. Old OpenMP requirements will not "
          "be handled",
          __PRETTY_FUNCTION__);
}
```

新机制是把 requires 编码进**主机符号表的一个条目**（`Flags == OMP_REGISTER_REQUIRES`，见 4.3.3），在 `registerLib` 里随镜像一起收集。这样做的好处是：requires 信息天然附属于二进制描述符，和镜像注册是同一条路径，无需额外的初始化时序约定。

#### 4.4.2 核心流程

requires 标志的语义在 [include/Shared/Requirements.h:24-42](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h#L24-L42) 定义为位掩码：

| 标志 | 含义 |
|------|------|
| `OMP_REQ_NONE` | 无 requires 指令 |
| `OMP_REQ_REVERSE_OFFLOAD` | 允许设备向主机反向卸载 |
| `OMP_REQ_UNIFIED_ADDRESS` | 统一地址 |
| `OMP_REQ_UNIFIED_SHARED_MEMORY` | 统一共享内存 |
| `OMP_REQ_DYNAMIC_ALLOCATORS` | 动态分配器 |
| `OMPX_REQ_AUTO_ZERO_COPY` | （扩展）APU 上自动零拷贝 |

收集逻辑由 `RequirementCollection::addRequirements`（[include/Shared/Requirements.h:60-95](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h#L60-L95)）实现，规则是：

```text
addRequirements(NewFlags):
  若 SetFlags 未定义:   SetFlags = NewFlags        // 第一个库直接设定
  否则若 SetFlags==NONE 且 NewFlags==AUTO_ZERO_COPY: SetFlags = NewFlags  // APU 特例
  否则:  对 reverse_offload / unified_address /
         unified_shared_memory / dynamic_allocators 逐项做一致性检查，
         不一致则 FATAL_MESSAGE 终止。
```

也就是「首个翻译单元设定基准，后续翻译单元必须与之一致」。这保证了链接在一起的不同编译单元不会提出互相矛盾的需求。

#### 4.4.3 源码精读

一致性检查的核心（[include/Shared/Requirements.h:49-56](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h#L49-L56)）：

```cpp
void checkConsistency(int64_t NewFlags, int64_t SetFlags,
                      OpenMPOffloadingRequiresDirFlags Flag,
                      llvm::StringRef Clause) {
  if ((SetFlags & Flag) != (NewFlags & Flag)) {
    FATAL_MESSAGE(2, "'#pragma omp requires %s' not used consistently!",
                  Clause.data());
  }
}
```

`PluginManager` 持有一个 `RequirementCollection Requirements`（[include/PluginManager.h:172](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L172)），并通过 `addRequirements` / `getRequirements` 转发（[include/PluginManager.h:138-141](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L138-L141)）。整个运行时通过 `PM->getRequirements()` 查询这些标志，例如 `loadImagesOntoDevice` 在判断是否为统一共享内存初始化指针时就用到它（[PluginManager.cpp:454-455](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L454-L455)）。

#### 4.4.4 代码实践

**目标**：理解 requires 一致性检查如何防止链接错误。

**步骤**：

1. 阅读 [Requirements.h:60-95](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/Requirements.h#L60-L95)，确认「首个库设定基准」的分支。
2. 假设有两个翻译单元 A、B：A 用了 `#pragma omp requires unified_shared_memory`，B 没用。两者链接进同一程序，运行时分别通过各自的 `__tgt_bin_desc` 调 `registerLib`。
3. **待本地验证**：推演 A 先注册时 `SetFlags` 被设为 `OMP_REQ_UNIFIED_SHARED_MEMORY`，B 后注册时 `checkConsistency` 发现 `(SetFlags & UNIFIED_SHARED_MEMORY) != (NewFlags & UNIFIED_SHARED_MEMORY)`，触发 `FATAL_MESSAGE` 报错并终止。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `__tgt_register_requires` 被废弃后，旧二进制调用它不会崩溃？
**答案**：运行时仍导出该符号，但函数体只打印一条警告信息（`MESSAGE(...)`），不做任何实际处理。这保证旧二进制能链接、能调用，只是它的 requires 需求不会被处理。

**练习 2**：`OMPX_REQ_AUTO_ZERO_COPY` 为何需要单独的特例分支，不能走一致性检查？
**答案**：它是运行时在设备初始化时**自己计算出来**的（APU 上由插件决定），而非用户用 `requires` 声明。它会在 `SetFlags == OMP_REQ_NONE`（用户没提任何需求）时由 `registerLib` 末尾追加（见 4.3.3），所以需要一个允许「从 NONE 跳到 AUTO_ZERO_COPY」的特例。

---

### 4.5 设备初始化与编号映射：`__tgt_init_all_rtls`

#### 4.5.1 概念说明

到目前为止，设备都是「按需初始化」的——只有当某块设备需要承接镜像或被使用时，才调 `initializePlugin` / `initializeDevice`。但有时用户想**一次性把所有可用设备都拉起来**，这就是 `__tgt_init_all_rtls` 的用途（例如 `omp_get_num_devices()` 需要所有设备可见时）。

[libomptarget/interface.cpp:101-104](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L101-L104)：

```cpp
EXTERN void __tgt_init_all_rtls() {
  assert(PM && "Runtime not initialized");
  PM->initializeAllDevices();
}
```

#### 4.5.2 核心流程：设备编号映射

多插件环境下，一个关键问题是：OpenMP 暴露给用户的「设备号 0,1,2,…」如何对应到「哪个插件的哪块卡」？答案在 `initializeDevice` 里——按初始化顺序给每块设备分配一个递增的 `UserId`，并建立 `(插件指针, 插件内设备号) → UserId` 的映射。

```text
initializeDevice(Plugin, DeviceId):
  若该设备已初始化: 标记 hasPendingImages，返回
  UserId = 当前设备容器大小          // 0,1,2,... 递增
  Device = new DeviceTy(&Plugin, UserId, DeviceId)
  Device->init()                     // 调插件初始化这块卡
  Devices.push_back(Device)
  DeviceIds[(Plugin, DeviceId)] = UserId   // 建立映射
```

设备容器 `Devices` 是被互斥保护的（`ProtectedObj<DeviceContainerTy>`，[include/PluginManager.h:177](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L177)），所有访问都要通过 `getExclusiveDevicesAccessor()` 拿到 RAII 访问器（[include/PluginManager.h:118-120](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L118-L120)）。

#### 4.5.3 源码精读

`initializeAllDevices`（[libomptarget/PluginManager.cpp:123-140](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L123-L140)）——遍历每个插件、每块设备，并注册一个 `atexit` 清理 interop 表：

```cpp
void PluginManager::initializeAllDevices() {
  for (auto &Plugin : plugins()) {
    if (!initializePlugin(Plugin))
      continue;
    for (int32_t DeviceId = 0; DeviceId < Plugin.number_of_devices(); ++DeviceId)
      initializeDevice(Plugin, DeviceId);
  }
  std::atexit([]() {
    if (PM)
      PM->InteropTbl.clear();
  });
}
```

`initializeDevice`（[libomptarget/PluginManager.cpp:87-121](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L87-L121)）——分配 `UserId`、构造 `DeviceTy`、建立映射：

```cpp
int32_t UserId = ExclusiveDevicesAccessor->size();
auto Device = std::make_unique<DeviceTy>(&Plugin, UserId, DeviceId);
if (auto Err = Device->init()) { ... return false; }
ExclusiveDevicesAccessor->push_back(std::move(Device));
PM->DeviceIds[std::make_pair(&Plugin, DeviceId)] = UserId;
```

`DeviceIds` 是 `DenseMap<pair<插件*, 设备号>, UserId>`（[include/PluginManager.h:161-162](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h#L161-L162)）。后续 `registerLib` 用它把「插件内的设备号」翻译成「OpenMP 用户设备号」（见 4.3.3 里的 `auto UserId = PM->DeviceIds[...]`）。

**延迟加载镜像**：设备虽然初始化了，但镜像并未立即加载。`getDevice` 在每次取设备时检查 `hasPendingImages()`，若有待加载镜像才调用 `loadImagesOntoDevice`（[libomptarget/PluginManager.cpp:553-573](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L553-L573)）：

```cpp
if (DevicePtr->hasPendingImages())
  if (loadImagesOntoDevice(*DevicePtr) != OFFLOAD_SUCCESS)
    return error::createOffloadError(...);
```

`loadImagesOntoDevice`（[libomptarget/PluginManager.cpp:390-551](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L390-L551)）才真正调 `Device.loadBinary(Img)`（[PluginManager.cpp:425](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L425)），解析出每个符号在设备上的地址，填进翻译表，并据此建立 `declare target` 全局变量的主机↔设备映射。

#### 4.5.4 代码实践

**目标**：理解 OpenMP 设备号到「插件 + 插件内设备号」的映射。

**步骤**：

1. 阅读 [PluginManager.cpp:87-121](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L87-L121)，记下 `UserId = ExclusiveDevicesAccessor->size()` 的含义。
2. 假设系统装了 host 插件（1 块 CPU 设备）和 CUDA 插件（2 块 GPU），且 host 插件先初始化。推演：
   - host 设备 0 → `UserId = 0`
   - CUDA 设备 0 → `UserId = 1`
   - CUDA 设备 1 → `UserId = 2`
3. 于是 `omp_get_num_devices()` 返回 3，用户用设备号 1、2 即可访问两块 GPU。
4. **待本地验证**：在装有多块同类 GPU 的机器上，用 [u1-l4](u1-l4-toolchain-and-run.md) 介绍的 `llvm-offload-device-info` 或 `omp_get_num_devices()` 观察设备总数与编号。

#### 4.5.5 小练习与答案

**练习 1**：`__tgt_init_all_rtls` 与 `registerLib` 里的 `initializeDevice` 有什么本质区别？
**答案**：`__tgt_init_all_rtls`（`initializeAllDevices`）**无条件**初始化所有插件的所有设备，不登记任何镜像；`registerLib` 里的 `initializeDevice` 是**按需**的——只为「能匹配到某份镜像」的设备做初始化。前者用于让所有设备对用户可见，后者用于把镜像绑定到兼容设备。

**练习 2**：为什么 `getDevice` 每次都要检查 `hasPendingImages()`？
**答案**：因为 `registerLib` 只把镜像登记进翻译表（`TargetsImages[UserId] = Img`），并没有立即加载。设备第一次被实际使用时，`getDevice` 发现还有待加载镜像，才调 `loadImagesOntoDevice` 真正 `loadBinary` 并填充符号表。之后 `setHasPendingImages(false)`（[PluginManager.cpp:539](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp#L539)），后续调用就跳过加载。这是一种「注册期登记、使用期加载」的延迟策略。

---

## 5. 综合实践

把本讲的内容串起来，完成下面这个**源码阅读 + 画图**任务。

**背景**：一个用 `clang -fopenmp -fopenmp-targets=nvptx64-nvidia-cuda` 编译的程序启动时，主机二进制里 Clang 生成的构造器带着一份 `__tgt_bin_desc *Desc` 调用了 `__tgt_register_lib(Desc)`。系统装了 host 插件和 CUDA 插件（1 块 GPU）。

**任务**：

1. 从 [interface.cpp:91](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/interface.cpp#L91) 开始，逐函数追踪到镜像被加载，画出一张完整的时序图，至少包含这些节点：
   - `__tgt_register_lib` → `initRuntime`（首次进入）→ `PM->init()` → `registerDelayedLibraries`
   - `PM->registerLib`：升级条目 → 收集 requires → 抽取镜像 → 双层匹配（host 插件不认 PTX，CUDA 插件认）→ `initializePlugin(CUDA)` → `initializeDevice(CUDA, 0)` → 登记翻译表
   - 后续某次 `target` 执行 → `PM->getDevice(0)` → `hasPendingImages()` 为真 → `loadImagesOntoDevice` → `Device.loadBinary` → 填充 `TargetsTable[0]`
2. 在图上用不同颜色或分区标出三个阶段：**运行时初始化**、**库注册（登记）**、**首次使用（加载）**。
3. 在图旁注明每个阶段分别持有哪些锁（`PluginMtx`、`RTLsMtx`、`TrlTblMtx`、设备容器的互斥锁）。

**预期结果**：你应该能得到一张清晰展示「运行时如何从一份二进制描述符出发，最终在设备上准备好可执行镜像」的全景图，并理解为什么注册和加载被刻意拆成两个阶段。

> 说明：本实践为源码阅读型实践，无需运行程序；若想在运行时验证各阶段，可在 Debug 构建下开启 `LIBOMPTARGET_DEBUG`（见 [u1-l4](u1-l4-toolchain-and-run.md)）观察 `Init offload library!`、`Registered plugin ...`、`Image ... is compatible with RTL ...`、`Registering image ...` 等日志的先后顺序。

## 6. 本讲小结

- `__tgt_rtl_init` / `__tgt_rtl_deinit` 只是对 `initRuntime` / `deinitRuntime` 的转发；运行时用 `RefCount` 实现「首次进入构造、末次退出销毁」，并用 `RTLAlive` / `RTLOngoingSyncs` 保证去初始化时没有正在进行同步。
- `__tgt_register_lib` 是最核心的注册入口，它「先确保运行时初始化、再（可能延迟）注册」；`delayRegisterLib` / `registerDelayedLibraries` 解决了插件初始化期间重入注册的时序竞态。
- `registerLib` 用「插件级 + 设备级」双层兼容判断，为每个设备镜像找到归属，并建立以 OpenMP 设备号 `UserId` 为下标的翻译表——但**只登记不加载**。
- `__tgt_register_requires` 已废弃；requires 标志现在随二进制描述符的一个特殊条目，在 `registerLib` 里收集，并由 `RequirementCollection` 强制跨翻译单元一致性。
- `__tgt_init_all_rtls` 无条件初始化所有插件的所有设备；`initializeDevice` 用递增的 `UserId` 建立「(插件, 插件内设备号) → OpenMP 设备号」的映射。
- 真正的镜像加载（`loadBinary`）被推迟到设备首次被 `getDevice` 使用时，由 `loadImagesOntoDevice` 完成，体现了「注册期登记、使用期加载」的按需设计。

## 7. 下一步学习建议

本讲建立了「运行时如何启动、如何登记镜像、如何给设备编号」的全局图景。接下来建议：

- 进入 [u2-l2 PluginManager 设备与插件管理](u2-l2-plugin-manager.md)，更系统地看 `PluginManager` 如何持有与枚举插件/设备、如何做互斥访问与 `Requirements` 收集。
- 阅读 [include/PluginManager.h](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/PluginManager.h) 与 [libomptarget/PluginManager.cpp](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/PluginManager.cpp) 里的 `loadImagesOntoDevice` 全文，为后续 [u2-l3 DeviceTy 设备抽象](u2-l3-device-abstraction.md) 做准备。
- 如果想先看「设备号拿到后如何使用」，可直接跳到 [u2-l5 target data 流程](u2-l5-target-data-flow.md) 与 [u2-l6 内核启动流程](u2-l6-kernel-launch-flow.md)，再回头对照本讲的初始化路径。
