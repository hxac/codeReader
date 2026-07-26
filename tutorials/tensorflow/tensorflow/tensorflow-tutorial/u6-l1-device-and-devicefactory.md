# Device 与 DeviceFactory

## 1. 本讲目标

在 u3-l2 中我们已经打通了 `DirectSession::Run` 的执行链路：一张图被剪枝、放置、分区，最终由若干个 `Executor` 在不同设备上跑起来。但你可能会问：**这里的「设备」到底是什么？GPU、CPU、TPU 是怎么被一一找出来、又是怎么被注册进运行时的？op 又是怎么被分配到某一台具体设备上的？**

本讲就回答这三个问题。学完后你应当掌握：

1. 理解 `Device` 作为「执行单元抽象」的本质，以及 `DeviceBase → Device → LocalDevice → ThreadPoolDevice/GPUDevice` 这条类继承链的分工。
2. 掌握 `DeviceFactory` 的工厂模式与「静态全局对象自动注册」机制，看懂 `REGISTER_LOCAL_DEVICE_FACTORY` 宏与优先级（priority）规则。
3. 认识 op 是如何被分配到具体设备的——从 `DeviceFactory::AddDevices` 收集设备、到 `DeviceSet` 排序、再到放置器（Placer）按优先级与 kernel 可用性做出选择。

---

## 2. 前置知识

- **工厂模式（Factory Pattern）**：把「创建对象」这件事单独抽出来。调用方不直接 `new` 具体类，而是问工厂要。这样运行时可以根据条件（比如本机有没有 GPU）决定造哪种设备，且新加一种设备时无需改动已有代码。
- **静态全局对象自动注册**：C++ 里，如果一个翻译单元（一个 `.cc` 文件）里定义了一个全局/静态对象，那么在 `main` 执行之前，它的构造函数就会被调用。TensorFlow 利用这一点，让每个设备工厂在自己被链接进二进制时，构造函数顺手把自己「登记」到一张全局表里——不需要谁在 `main` 里显式写 `register(...)`。这一招在 u4-l1（`REGISTER_OP`）、u1-l5（`SessionFactory`）里反复出现，是阅读 TF 源码的关键心法。
- **抽象类与纯虚函数**：C++ 用 `virtual ... = 0` 定义「接口」。子类必须实现这些纯虚函数才能被实例化。本讲里 `Device`、`DeviceFactory` 都是抽象基类。
- **设备命名格式**：复习 u3-l1/u3-l2 出现过的设备全名格式 `/job:___/replica:___/task:___/device:(CPU|GPU|TPU):___`，例如 `/job:localhost/replica:0/task:0/device:CPU:0`。本讲会解释它的解析。

> 术语提示：本讲出现的 `Device`、`DeviceBase`、`DeviceFactory`、`DeviceSet`、`Placer`、`priority`、`incarnation` 都是 TF 运行时的核心术语，下文会逐一在真实代码处展开。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/core/framework/device_base.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_base.h) | 定义 `DeviceBase`——最底层的「设备能力骨架」，提供线程池、Allocator、Eigen 设备等抽象（大多是空实现/LOG(FATAL)）。 |
| [tensorflow/core/framework/device.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device.h) / [device.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device.cc) | 定义 `Device : public DeviceBase`，在 `DeviceBase` 之上补齐「名字、属性、`Compute`、`Sync`、资源管理」等真正面向 Session 的接口。 |
| [tensorflow/core/framework/device_factory.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.h) / [device_factory.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.cc) | 定义 `DeviceFactory` 抽象工厂、全局注册表、`REGISTER_LOCAL_DEVICE_FACTORY` 宏，以及 `AddDevices`/`GetFactory`/`DevicePriority` 等静态方法。 |
| [tensorflow/core/common_runtime/threadpool_device_factory.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/threadpool_device_factory.cc) | CPU 设备的具体工厂 `ThreadPoolDeviceFactory`，是最典型的「实现一个新设备工厂」的样板。 |
| [tensorflow/core/common_runtime/threadpool_device.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/threadpool_device.h) / [local_device.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/local_device.h) | CPU 设备具体类 `ThreadPoolDevice` 及其父类 `LocalDevice`，展示完整继承链。 |
| [tensorflow/core/common_runtime/device_set.h](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/device_set.h) / [device_set.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/device_set.cc) | `DeviceSet`：把工厂造出的一堆 `Device` 收集起来，并按优先级排序，供放置器查询。 |
| [tensorflow/core/common_runtime/direct_session.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc) | 在 `DirectSession` 构造时调用 `DeviceFactory::AddDevices`，是设备被发现与创建的「现场」。承接 u3-l2。 |

---

## 4. 核心概念与源码讲解

### 4.1 Device 抽象与类继承体系（core.framework.device）

#### 4.1.1 概念说明

在 TensorFlow 里，**Device（设备）是「能执行 op 计算的东西」的抽象**。它既可以是本机的 CPU、GPU、TPU，也可以是远端另一台机器上的设备（通过 RPC 联系）。一句话：图里的每个节点最终都要落到某个 Device 上由它来「算」。

源码头注释把这件事说得很直白：

> A Device is a something that can perform computations as part of a model.

注意 Device 抽象刻意**只描述「能力」，不绑定具体硬件**。于是它被拆成两层继承：

- **`DeviceBase`**：最底层骨架，定义「一个设备大概需要哪些能力」——环境（`Env`）、CPU 工作线程、加速器信息、内存分配器 `Allocator`、Eigen 计算设备等。绝大多数是带默认空实现的虚函数，留给具体子类去填。
- **`Device : public DeviceBase`**：在 `DeviceBase` 之上补齐「面向 Session 的接口」——设备名字、属性、`Compute`、`Sync`、`ResourceMgr`、`OpSegment` 等。这才是 `DirectSession`、`Executor` 真正打交道的对象。

具体硬件设备则继续往下派生，例如 CPU 走 `ThreadPoolDevice → LocalDevice → Device`，GPU 走 `BaseGPUDevice → Device`。

#### 4.1.2 核心流程：一条继承链

```
DeviceBase            （能力骨架：线程池/Allocator/Eigen/属性默认实现）
   ▲
   │ public
   │
  Device               （面向 Session：name/attributes/Compute/Sync/ResourceMgr）
   ▲
   │ public
   │
 LocalDevice           （CPU 与 GPU 共享：Eigen 线程池初始化）
   ▲
   │ public
   │
ThreadPoolDevice       （CPU 具体实现：填好 Allocator/Compute/Sync 等）
```

这条链的分层哲学是：

1. `DeviceBase` 负责「策略无关的底座」——谁都需要线程池、谁都需要分配器，但具体实现各自不同，所以只给接口。
2. `Device` 负责「与执行框架对接」——`Compute(OpKernel*, OpKernelContext*)` 是执行 op 的总入口，`Sync()` 是同步屏障，`ResourceMgr` 存放跨 step 共享的资源（如变量）。
3. `LocalDevice`/`ThreadPoolDevice` 负责「填空」——把基类里 `LOG(FATAL)` 的方法用真实硬件实现补上。

#### 4.1.3 源码精读

**`DeviceBase` 的能力清单**——注意 `GetAllocator` 默认直接 `LOG(FATAL)`，说明它是个「待子类实现」的占位：

[`tensorflow/core/framework/device_base.h:L193-L196`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_base.h#L193-L196) 说明 `DeviceBase::GetAllocator` 默认是致命错误，强制具体设备重写它来提供真正的内存分配器。

它还声明了一组「身份」虚函数（`attributes()`/`name()`/`device_type()`），默认实现大多需要在 `Device` 里覆盖：

[`tensorflow/core/framework/device_base.h:L239-L243`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_base.h#L239-L243) 给出设备的基本身份信息（属性、NUMA 节点、名字、解析后的名字、设备类型）。

**`Device` 的核心契约**——`Compute` 直接转交给 `OpKernel`（默认实现），`Sync` 则是纯虚，必须由子类实现：

[`tensorflow/core/framework/device.h:L88-L105`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device.h#L88-L105) 说明：`Compute` 默认实现就是把活儿转给 `op_kernel->Compute(context)`（回顾 u4-l2 的 OpKernel::Compute）；而 `Sync() = 0` 是纯虚，意味着没有默认实现，任何具体 Device 都得自己定义「如何等待本设备上排队的操作全部完成」。

设备名字是设备最关键的身份标识。看构造函数如何解析它：

[`tensorflow/core/framework/device.cc:L27-L32`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device.cc#L27-L32) 说明：构造 `Device` 时会强制用 `DeviceNameUtils::ParseFullName` 解析名字，名字不合法直接 `CHECK` 失败；同时按 `parsed_name_.job` 创建一个 `ResourceMgr`，用于存放本设备上的共享资源（如变量）。这解释了为什么每个设备都有一个独立资源管理器。

每个设备还有一个全局唯一的「化身号（incarnation）」——它在本讲只是设备属性的一部分，但记住它是用随机数生成的、且保证非零：

[`tensorflow/core/framework/device.cc:L43-L57`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device.cc#L43-L57) 说明 `BuildDeviceAttributes` 用 `do/while` 反复生成随机数直到非零来设置 `incarnation`——这是设备在跨进程/跨重启场景下的稳定身份标识（例如 `_Recv` 用它来判断发送方是否换了进程）。

**CPU 设备具体类**——`ThreadPoolDevice` 只是把基类的占位补全：

[`tensorflow/core/common_runtime/threadpool_device.h:L25-L50`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/threadpool_device.h#L25-L50) 说明 `ThreadPoolDevice` 继承 `LocalDevice`，并重写了 `GetAllocator`、`MakeTensorFromProto`、`CopyTensorInSameDevice`，以及关键的 `Compute`/`ComputeAsync` 和 `Sync`（CPU 的 `Sync` 永远返回 OK，因为 CPU 计算是同步完成的）。

它的 `Compute` 真正「干活」的地方：

[`tensorflow/core/common_runtime/threadpool_device.cc:L177-L197`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/threadpool_device.cc#L177-L197) 说明 CPU 设备的 `Compute` 主要做了输入输出日志（调试用），核心仍是调用 `op_kernel->Compute(context)`——也就是说「谁来算」与「怎么算」是解耦的：Device 负责调度与环境，OpKernel 负责算法。

#### 4.1.4 代码实践

**实践目标**：用一段 Python，亲眼看到「设备身份」由哪些字段构成，建立对 `DeviceAttributes` 的直觉。

**操作步骤**（已安装 TensorFlow 时）：

```python
import tensorflow as tf
for d in tf.config.list_physical_devices():
    print(d)
for d in tf.config.list_logical_devices():
    print(d.name, d.device_type)
```

**需要观察的现象**：列表里一定有至少一个 `PhysicalDevice(name='/physical_device:CPU:0', device_type='CPU')`；如果本机有 GPU，还会看到 `GPU` 条目。

**预期结果**：CPU 永远存在（后面 4.3 会解释「CPU is required」）。设备的「物理名」格式 `/physical_device:CPU:0` 正对应 `threadpool_device_factory.cc` 里 `ListPhysicalDevices` 推入的字符串。incarnation 等字段可用 `tf.config.experimental.get_device_details`（仅对部分设备可用）观察。若本机未装 GPU 版 TF，则只有 CPU——这本身也印证了「设备由工厂按本机能力创建」。

> 若运行环境未安装 TensorFlow，本步骤为「待本地验证」；命令本身是标准用法。

#### 4.1.5 小练习与答案

**练习 1**：`Device::Sync()` 为什么设计成纯虚函数，而 `Compute()` 给了默认实现？

**参考答案**：`Compute` 的默认实现「把活儿转给 OpKernel」对所有设备都成立（无论 CPU/GPU 都是调 `op_kernel->Compute(context)`），所以可以给默认实现；而「如何等待设备上排队的操作完成」因硬件而异——CPU 是同步的（立刻返回 OK），GPU 要等 stream 刷空，TPU 又是另一套——没法给统一默认，故设为纯虚，强制子类各自定义。

**练习 2**：`DeviceBase` 里 `GetAllocator` 默认 `LOG(FATAL)`，这种「故意崩溃的默认实现」有什么好处？

**参考答案**：它把「必须由子类实现」这一契约编译期无法表达的要求，转成运行期的强约束——任何忘记重写 `GetAllocator` 的具体设备类，一旦真的被调用就会立刻崩溃报错，而不是静默返回空指针导致后续难以排查的空指针 bug。这是 TF 里常见的「防御性默认」风格。

---

### 4.2 DeviceFactory 注册与设备创建（core.framework.device_factory）

#### 4.2.1 概念说明

知道了 `Device` 是什么，下一个问题是：**运行时怎么知道这台机器上有哪些设备？** 答案是 `DeviceFactory`。

`DeviceFactory` 采用**工厂模式 + 静态自动注册**：

- 每一种设备类型（CPU/GPU/TPU/XLA-CPU…）写一个工厂子类，实现 `CreateDevices`（按本机能力造出若干 `Device` 对象）。
- 这个子类用一个宏 `REGISTER_LOCAL_DEVICE_FACTORY("CPU", ThreadPoolDeviceFactory, 60)` 在程序启动时把自己登记到一张全局表里。
- 运行时（比如 `DirectSession` 构造时）调 `DeviceFactory::AddDevices(...)`，它会遍历这张全局表，让每个工厂各自造设备。

关键设计点：**注册表是「设备类型字符串 → 工厂」的映射，且支持「同类型多工厂按优先级竞争」**。同一种 `device_type` 可以注册多个工厂，优先级高的胜出。这就是为什么 GPU 版 TF 里既有 `ThreadPoolDeviceFactory`（CPU，priority 60）又有 `GPUCompatibleCPUDeviceFactory`（也注册成 "CPU"，priority 70）——后者优先级更高会被选中。

#### 4.2.2 核心流程

整个机制可以用三步概括：

```
① 启动期自动注册（main 之前）
   每个 REGISTER_LOCAL_DEVICE_FACTORY(...) 宏
        └─ 构造一个静态全局 dfactory::Registrar<Factory> 对象
             └─ 其构造函数调用 DeviceFactory::Register(type, make_unique<Factory>(), priority, is_pluggable)
                  └─ 写入全局 device_factories() 表（带 mutex 保护、优先级裁决）

② 运行期收集设备（如 DirectSession 构造时）
   DeviceFactory::AddDevices(options, name_prefix, &devices)
        ├─ AddCpuDevices()  →  GetFactory("CPU")->CreateDevices(...)   （CPU 必须最先且必须有）
        └─ 遍历其余工厂      →  factory->CreateDevices(...)
             每个工厂依据 options.config.device_count() 与本机硬件，造出 N 个 Device 放进 devices

③ 查询优先级（供放置器排序）
   DeviceFactory::DevicePriority("GPU")  →  读表返回 210
        ↑
        DeviceSet::DeviceTypeOrder(d)  就是转调它
```

优先级裁决规则（在 `Register` 里）值得单独记住：

- 表中没有该 `device_type`：直接插入。
- 已有同类型、新优先级更高：用新工厂覆盖旧的。
- 已有同类型、新优先级相同：`LOG(FATAL)`——禁止两个工厂并列，逼你显式分出高下。
- 已有同类型、新优先级更低：忽略新的。

默认优先级（见 `device_factory.h` 注释）：GPU=210、GPUCompatibleCPU=70、ThreadPoolDevice=60、默认=50。所以 **GPU 默认优先于 CPU**——这正是放置器在没有显式指定设备时偏好 GPU 的根因。

此外，注册还受环境变量 `TF_ENABLED_DEVICE_TYPES` 控制：若它非空，则只有列在其中的设备类型才会真正注册，其余被静默禁用。这提供了一种「不重新编译就能关掉某类设备」的开关。

#### 4.2.3 源码精读

**全局注册表与优先级裁决**——这是整个机制的「心脏」：

[`tensorflow/core/framework/device_factory.cc:L50-L54`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.cc#L50-L54) 说明全局注册表是一个函数内 `static` 的 `unordered_map<string, FactoryItem>`（`FactoryItem` = 工厂 + 优先级 + 是否 pluggable）。用函数局部 static 而非全局变量，是为了规避跨翻译单元初始化顺序问题（Meyers Singleton）。

[`tensorflow/core/framework/device_factory.cc:L56-L66`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.cc#L56-L66) 说明 `IsDeviceFactoryEnabled` 读 `TF_ENABLED_DEVICE_TYPES` 环境变量来决定某类设备是否允许注册——这就是「运行期按需开关设备」的实现。

[`tensorflow/core/framework/device_factory.cc:L92-L114`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.cc#L92-L114) 说明 `Register` 的完整优先级裁决逻辑：禁用则跳过；否则加锁后按「无则插、高则覆盖、等则 FATAL、低则忽略」四条规则更新表。这是 4.2.2 流程图里第①步的核心。

**自动注册宏与 `Registrar` 模板**——这是「写一行就完成注册」的魔法所在：

[`tensorflow/core/framework/device_factory.h:L119-L156`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.h#L119-L156) 说明 `dfactory::Registrar<Factory>` 是个模板类，构造函数里 `make_unique<Factory>()` 造出工厂实例并调 `DeviceFactory::Register`。注释详细解释了优先级的两种用途：(1) 同 `device_type` 多工厂时选优先级最高者；(2) 在 `DeviceSet` 里决定哪种设备类型更受偏好。这段注释里还列出了内置设备的默认优先级（GPU 210 等）。

[`tensorflow/core/framework/device_factory.h:L160-L170`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.h#L160-L170) 说明 `REGISTER_LOCAL_DEVICE_FACTORY` 宏展开后，会在该 `.cc` 文件里生成一个「静态全局 `Registrar` 对象」。因为它在文件作用域是 `static`，其构造函数在 `main` 之前运行——这就是「自动注册」的物理基础。`__COUNTER__` 用来给每个注册点生成唯一变量名，防止重复注册冲突。

**CPU 工厂——最佳实现样板**：

[`tensorflow/core/common_runtime/threadpool_device_factory.cc:L36-L81`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/threadpool_device_factory.cc#L36-L81) 说明一个最小可用的 `DeviceFactory` 子类长什么样：实现 `ListPhysicalDevices`（报告 `/physical_device:CPU:0`）和 `CreateDevices`（按 `device_count["CPU"]`、结合 NUMA 亲和性造出若干 `ThreadPoolDevice`），最后用 `REGISTER_LOCAL_DEVICE_FACTORY("CPU", ThreadPoolDeviceFactory, 60)` 把自己注册成 "CPU" 类型、优先级 60。新增任何设备类型，照此三件套照搬即可。

**设备收集入口 `AddDevices`**：

[`tensorflow/core/framework/device_factory.cc:L230-L264`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.cc#L230-L264) 说明 `AddDevices` 的策略：**CPU 必须最先加且必须有**（`AddCpuDevices` 找不到 CPU 工厂会报 `NotFoundError`）；随后遍历其余工厂，遵守 `options.config.device_filters()` 过滤，让每个工厂各自 `CreateDevices`。这就是「同一进程内 CPU/GPU/TPU 设备被一次性枚举」的统一入口。

#### 4.2.4 代码实践

**实践目标**：对照 `device_factory.h`，说明一个新设备类型（如自定义加速器「APU」）应如何通过工厂注册到运行时。

**操作步骤（源码阅读型实践）**：

1. 打开 [tensorflow/core/common_runtime/threadpool_device_factory.cc](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/threadpool_device_factory.cc) 作为模板。
2. 想象你要加一个 APU 设备，按下面四步对照源码填出「需要做什么」：

   | 步骤 | 要做的事 | 对应源码依据 |
   | --- | --- | --- |
   | ① 写 Device 子类 | 实现 `Compute`/`Sync`/`GetAllocator`/`MakeTensorFromProto` 等 | `threadpool_device.h:L25-L50` 的重写清单 |
   | ② 写 DeviceFactory 子类 | 实现 `ListPhysicalDevices` 与 `CreateDevices`，在 `CreateDevices` 里 `make_unique<ApuDevice>` 推入 `devices` | `threadpool_device_factory.cc:L36-L79` |
   | ③ 注册工厂 | 文件末尾写 `REGISTER_LOCAL_DEVICE_FACTORY("APU", ApuDeviceFactory, 220)`（优先级给高一点，比如 >210，让放置器偏好它） | `threadpool_device_factory.cc:L81` |
   | ④ 链接进二进制 | 把该 `.cc` 加入相应 Bazel target，确保被链接进 `import tensorflow` 背后的 `.so` | `REGISTER_LOCAL_DEVICE_FACTORY` 靠静态全局对象在 `main` 前自动登记，无需手写 init |

3. 真实测试里的极简范例可参考 `direct_session_test.cc`：[tensorflow/core/common_runtime/direct_session_test.cc:L2524-L2525](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session_test.cc#L2524-L2525) 注册了 `FakeFactory<'A'>` 为 "APU"、`FakeFactory<'Z'>` 为 "ZPU"——这正是「用一个假工厂验证多设备注册与放置」的官方做法。

**需要观察的现象**：理论上注册成功后，`tf.config.list_physical_devices()` 应能看到 "APU"；放置器在没有显式指定设备时，因 priority=220 > GPU 210，会把支持 APU 的 op 放到 APU 上。

**预期结果**：本机若无真实 APU，`CreateDevices` 会返回 0 个设备，于是 `list_physical_devices` 看不到它，但工厂本身已注册（`GetFactory("APU")` 能查到）。这正说明「注册工厂」与「本机真有该硬件」是两件事。

> 本实践为源码阅读型，无需运行；若要真正跑通，需在具备 APU kernel 注册（见 u4-l2）的前提下重新编译 TF。

#### 4.2.5 小练习与答案

**练习 1**：为什么 GPU 版 TF 要同时注册两个 "CPU" 工厂（`ThreadPoolDeviceFactory` priority=60 与 `GPUCompatibleCPUDeviceFactory` priority=70）？

**参考答案**：GPU 版里 CPU 设备需要「与 GPU 协作」的能力（如正确的内存拷贝、与 stream 配合的 Allocator），所以提供了一个功能更强的 `GPUCompatibleCPUDeviceFactory`，用更高优先级 70 覆盖普通的 60。于是 `GetFactory("CPU")` 返回的是更强的那个——这就是「同类型多工厂按优先级竞争」机制的现实用例。

**练习 2**：`device_factories()` 为什么写成「函数内 static 局部变量」而不是一个普通的命名空间级全局 `std::unordered_map`？

**参考答案**：因为不同 `.cc`（CPU 工厂、GPU 工厂、XLA 工厂…）里的静态 `Registrar` 对象会在 `main` 前各自构造并往表里写，而 C++ 跨翻译单元的全局对象初始化顺序是未定义的。把表藏进函数局部 static（Meyers Singleton），保证「第一次调用该函数时才初始化」，从而在任意 `Registrar` 构造时该表都已完成构造、可安全使用。配合 `get_device_factory_lock()` 互斥锁保证线程安全。

---

### 4.3 op 如何被分配到具体设备（从工厂到放置）

#### 4.3.1 概念说明

前两节解决了「设备怎么来」。最后要回答本讲第三个学习目标：**图里的每个 op，是怎么被分配到某一台具体设备的？**

这个流程串起来是：

```
DeviceFactory::AddDevices  →  一堆 Device 对象
        ↓
   DeviceSet（收集 + 按优先级排序）
        ↓
   Placer 放置器（对每个 node：综合「优先级 + kernel 可用性 + 显式 device 约束 + colocation」选出一台 Device）
        ↓
   每个 node 落到具体 Device 上
```

也就是说，工厂阶段决定了「候选设备池与偏好顺序」，真正给每个 op 定位的是放置器（Placer，u3-l2 里提到的「Placement 放置」阶段）。本节聚焦「设备偏好顺序」是怎么从工厂的优先级一路传到放置器的。

#### 4.3.2 核心流程：优先级如何流向放置

1. **设备枚举**：`DirectSession` 构造时调 `DeviceFactory::AddDevices(...)`，拿到本进程全部设备（见 4.2）。
2. **装进 DeviceSet**：这些 `Device*` 被 `DeviceSet::AddDevice` 收集，并构建 `device_by_name_`（全名→设备）映射；还会标记一个 `client_device`（本进程的代表设备）。
3. **按优先级排序**：`DeviceSet` 用 `PrioritizedDeviceTypeList()` / `prioritized_devices()` 给设备排序，排序依据是 `DeviceTypeOrder`。
4. **放置器查询**：放置器对每个节点，在满足「该设备上有对应 kernel（见 u4-l2 的 KernelRegistry）」「显式 `with tf.device(...)` 约束」「`colocate_with` 约束」等条件的前提下，按这个优先级顺序选第一个合适的设备。

关键链路（已用源码验证）：

\[ \text{注册优先级} \xrightarrow{\text{DeviceFactory::DevicePriority}} \text{DeviceSet::DeviceTypeOrder} \xrightarrow{\text{排序}} \text{放置器偏好} \]

即：**你在工厂注册时填的 priority 数字，最终决定了「没有显式指定设备时，op 倾向于落到哪种设备上」**。GPU 的 210 > CPU 的 60，所以默认偏好 GPU。

#### 4.3.3 源码精读

**现场：DirectSession 在哪收集设备**——承接 u3-l2：

[`tensorflow/core/common_runtime/direct_session.cc:L239-L243`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L239-L243) 说明 `DirectSession` 的工厂方法里，先用 `DeviceFactory::AddDevices(options, "/job:localhost/replica:0/task:0", &devices)` 枚举本进程所有设备，再把这些 `unique_ptr<Device>` 交给新建的 `DirectSession`。`name_prefix` 决定了设备的全名前缀（本地会话固定为 localhost）。

**DeviceSet：收集与排序容器**：

[`tensorflow/core/common_runtime/device_set.h:L33-L68`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/device_set.h#L33-L68) 说明 `DeviceSet` 持有 `devices_`（不拥有所有权）、`device_by_name_`（按全名查找）、`client_device_`，并提供 `FindMatchingDevices`/`FindDeviceByName`/`PrioritizedDeviceTypeList`。它是放置器与执行器共同查询的「设备清单」。

**优先级如何穿透到排序**——这是把 4.2 与放置器缝起来的那一行：

[`tensorflow/core/common_runtime/device_set.cc:L69-L72`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/device_set.cc#L69-L72) 说明 `DeviceSet::DeviceTypeOrder(d)` 直接转调 `DeviceFactory::DevicePriority(d.type_string())`——也就是说，`DeviceSet` 对设备类型的偏好顺序，**完全来自工厂注册时的 priority**。这是本讲最重要的「接线点」之一。

[`tensorflow/core/common_runtime/device_set.cc:L120-L131`](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/device_set.cc#L120-L131) 说明在 `SortPrioritizedDeviceVector` 里，当两个设备类型不同时，先比显式 priority，再比 `DeviceFactory::DevicePriority` 算出的优先级——再次确认工厂 priority 是设备排序的最终依据。

> 放置器（Placer）本身位于 `core/common_runtime/placer.cc`，它消费 `DeviceSet` 的优先级、结合 kernel 注册与约束为每个 node 选设备。其内部细节超出本讲范围（承接 u3-l2 的放置阶段），此处只需记住「优先级供给方」是 `DeviceFactory`。

#### 4.3.4 代码实践

**实践目标**：验证「设备优先级」对 op 放置的实际影响。

**操作步骤（已安装 GPU 版 TensorFlow 时）**：

```python
import tensorflow as tf

# 1) 看候选设备池（对应 DeviceFactory::AddDevices 的产物）
print(tf.config.list_physical_devices())

# 2) 不显式指定设备，观察一个 op 默认落到哪
@tf.function
def f(x):
    return tf.matmul(x, x)        # 矩阵乘，CPU/GPU 都有 kernel

with tf.device(None):              # 不约束设备
    print(f(tf.ones((2, 2))).device)   # 打印结果张量所在设备
```

**需要观察的现象**：

- 在 GPU 机器上，`f(...).device` 通常形如 `/job:localhost/replica:0/task:0/device:GPU:0`，即默认落到了 GPU——因为 GPU priority=210 > CPU=60。
- 用 `with tf.device('/CPU:0'):` 显式约束后，结果会落到 CPU——这对应放置器里「显式 device 约束」优先于「优先级偏好」。

**预期结果**：印证「工厂注册的 priority 决定了无约束时的设备偏好」。若本机为纯 CPU 环境，则只能看到 CPU，无法对比——此时该现象为「待本地验证」。

**对应源码**：把上面打印出的设备名，回看 [device_factory.cc 的 AddDevices](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/framework/device_factory.cc#L230-L264) 与 [device_set.cc 的 DeviceTypeOrder](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/device_set.cc#L69-L72)，即可在脑子里走通「注册 → 收集 → 排序 → 放置」全链路。

#### 4.3.5 小练习与答案

**练习 1**：如果想让某个自定义加速器在无显式设备约束时成为默认首选，注册时该怎么做？

**参考答案**：在 `REGISTER_LOCAL_DEVICE_FACTORY("APU", ApuDeviceFactory, P)` 里把 `P` 设成一个大于 210（GPU 默认优先级）的数，比如 220。这样 `DeviceFactory::DevicePriority("APU")` 返回 220，经 `DeviceSet::DeviceTypeOrder` 传到放置器后，APU 就会在设备类型偏好排序里排在 GPU 之前，只要该 op 有 APU kernel（见 u4-l2），就会被放到 APU。

**练习 2**：`DeviceFactory::AddDevices` 为什么强制「CPU 必须最先加且必须有」？

**参考答案**：因为放置与回退（fallback）需要一个「永远可用」的设备兜底——CPU 总是存在的，且绝大多数 op 都有 CPU kernel（见 u4-l2 的 KernelRegistry）。若连 CPU 都没有，遇到任何无法放到加速器上的 op 就无处可去了。代码里 `ListAllPhysicalDevices` 与 `AddCpuDevices` 找不到 CPU 工厂都会返回 `NotFoundError("CPU Factory not registered. Did you link in threadpool_device?")`，强制这条不变量。

---

## 5. 综合实践

**任务**：为一个虚构的「NPU」加速器，画出从「写代码」到「op 落到 NPU」的完整链路图，并把每一步标注到真实源码文件与行号。

要求按下面 5 个阶段产出一份文字说明（含一张流程图）：

1. **设备类**：参照 `threadpool_device.h` 列出 `NpuDevice` 需要重写的方法清单（至少 `Compute`/`Sync`/`GetAllocator`/`MakeTensorFromProto`），并说明 `Sync` 在 NPU 上大致要做什么（等 NPU stream/command queue 刷空）。
2. **工厂类**：参照 `threadpool_device_factory.cc`，写出 `NpuDeviceFactory` 必须实现的方法（`ListPhysicalDevices`、`CreateDevices`）以及 `CreateDevices` 里如何依据 `options.config.device_count()["NPU"]` 决定造几个设备。
3. **注册**：写出注册宏调用 `REGISTER_LOCAL_DEVICE_FACTORY("NPU", NpuDeviceFactory, 220)`，并解释为什么选 220（要 > GPU 的 210 才能成为默认首选）。
4. **收集**：说明 `DirectSession` 构造时经 [direct_session.cc:L240-L241](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/direct_session.cc#L240-L241) 的 `DeviceFactory::AddDevices` 会自动调用你的工厂。
5. **放置**：说明 [device_set.cc:L69-L72](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/device_set.cc#L69-L72) 如何把你的 priority 220 传给放置器，使无约束的 op 默认落到 NPU（前提是 NPU 有对应 kernel，见 u4-l2）。

**预期产出**：一张能自洽回答「NPU 是怎么进到 TF 里的、op 怎么落到它上面」的流程图，且每个箭头都能对应到本讲引用的某个源码位置。这一任务把本讲三个最小模块（Device 抽象、DeviceFactory 注册、op→设备分配）串成了一条完整闭环。

---

## 6. 本讲小结

- `Device` 是「执行单元抽象」，继承链为 `DeviceBase → Device → LocalDevice → ThreadPoolDevice/GPUDevice`；`DeviceBase` 给能力骨架（多为 `LOG(FATAL)` 占位），`Device` 补齐面向 Session 的接口（`Compute`/`Sync`/`ResourceMgr`/名字与属性）。
- `Device::Compute` 默认转交给 `OpKernel::Compute`，体现「谁来算（Device）」与「怎么算（OpKernel）」解耦；`Sync` 因硬件而异被设为纯虚。
- 设备命名遵循 `/job:__/replica:__/task:__/device:(CPU|GPU|TPU):__`，构造时强制解析并据此建独立 `ResourceMgr`；每个设备还有非零的随机 `incarnation` 作为跨进程身份。
- `DeviceFactory` 用工厂模式 + 静态全局对象自动注册：宏 `REGISTER_LOCAL_DEVICE_FACTORY` 在 `main` 前把工厂写入函数局部 static 的全局表 `device_factories()`（Meyers Singleton + 互斥锁）。
- 同一 `device_type` 可注册多个工厂，按优先级裁决（高覆盖低、相等 FATAL）；默认优先级 GPU=210 > GPUCompatibleCPU=70 > ThreadPool CPU=60 > 默认 50；`TF_ENABLED_DEVICE_TYPES` 可运行期禁用某类设备。
- op 落到设备经历「`AddDevices` 收集 → `DeviceSet` 排序 → Placer 选择」，其中排序依据 `DeviceSet::DeviceTypeOrder` 直接转调 `DeviceFactory::DevicePriority`——即工厂注册时的 priority 最终决定了无约束时 op 的设备偏好。

---

## 7. 下一步学习建议

- **设备上的内存**：本讲多次出现 `GetAllocator`，下一讲 **u6-l2 Allocator 与 BFCAllocator 内存管理** 将深入张量内存如何被分配与碎片整理，是理解 Device 能力骨架的关键续篇。
- **图优化阶段**：设备确定后，Grappler 会在图执行前做变换，详见 **u6-l3 Grappler 图优化器**。
- **分布式设备**：本讲的设备是「本地设备」，**u6-l4 分布式策略 distribute** 将展开多机多卡场景下设备如何被组织成 cluster、worker、task。
- **延伸阅读**：可对照阅读 `core/common_runtime/placer.cc`（放置器如何综合优先级、kernel 可用性与约束选设备）与 `core/common_runtime/device_set.cc`（排序的完整实现），把本讲的「优先级供给」一端补全为「优先级消费」一端。
