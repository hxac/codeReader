# 编译器-运行时契约与核心数据结构

## 1. 本讲目标

本讲要回答一个关键问题：**Clang 把一段 `#pragma omp target` 代码编译成了什么，运行时（libomptarget）又凭什么能看懂它？**

这二者之间的“约定”就叫做 **编译器-运行时契约（compiler–runtime contract）**。它不是一份文档，而是几个写死在头文件里的数据结构与 C 函数签名。学完本讲你应当能够：

1. 说清 Clang 在主机代码里生成了哪几类 `__tgt_*` 调用，以及它们各自对应运行时的哪个入口。
2. 看懂 `tgt_map_type` 这一大串位标志，知道 `map(to:)` / `map(from:)` / `map(always:)` 最终被编码成什么数字。
3. 理解 `__tgt_bin_desc`、`__tgt_device_image`、`__tgt_target_table` 这三件套如何“描述一张卸载镜像”，以及 `EntryTy` 这种 offload entry 条目记录了什么。

本讲只读头文件、建立“数据结构地图”，**不涉及运行时内部实现**（那属于第二单元）。

## 2. 前置知识

在进入源码前，先用三段白话补齐背景。承接 [u1-l1 项目定位](u1-l1-project-overview.md) 与 [u1-l3 目录结构](u1-l3-directory-map.md) 已建立的认知：

- **设备镜像（device image）不是运行时翻译出来的。** 在 [u1-l1](u1-l1-project-overview.md) 我们强调过：编译期 Clang 就已经为目标设备（CPU / NVIDIA / AMD / Intel GPU）各自生成了一份二进制镜像。运行时**不翻译代码**，它只负责“搬运数据 + 把这份现成的镜像丢给设备执行”。因此运行时必须有一套语言来描述“镜像在哪、有多大、里面有哪些可调用入口”，这就是本讲要讲的二进制描述符。
- **主机代码和设备代码是两套。** Clang 一次编译会产出主机对象（含普通主机代码 + 对运行时的 `__tgt_*` 调用）和设备镜像。`__tgt_*` 是“主机侧”的 C 函数，定义在 `libomptarget.so` 里；它和你在 [u1-l3](u1-l3-directory-map.md) 看到的调用链“编译器生成 `__tgt_*` → `interface.cpp` → `PM` 单例 → `DeviceTy` → 插件 → 设备”是同一条路。
- **“契约”用 C ABI 表达。** Clang 和 libomptarget 是两个独立的编译产物，它们靠一组 `extern "C"` 的结构体和函数签名“对接”。只要双方都遵守 `include/omptarget.h` 与 `include/Shared/APITypes.h` 里的定义，就能版本解耦地协作。

> 一个贯穿全讲的直觉：本讲的全部数据结构，本质都是**两张“表”和一段“镜像字节”**——镜像字节是设备代码本身，两张表分别是“镜像里有哪些入口”和“这次调用要搬运哪些主机变量”。把它记牢，下面的结构体就不会乱。

## 3. 本讲源码地图

本讲只锚定三个头文件，全部位于 `include/` 契约层（[u1-l3](u1-l3-directory-map.md) 把它定位为“编译器与运行时共用的契约层”）：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`include/omptarget.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h) | 契约主头文件，`__tgt_*` 系列入口的 C 签名 | `__tgt_*` 函数族、`tgt_map_type`、`TargetAllocTy` |
| [`include/Shared/APITypes.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h) | 用户代码 / 运行时 / 插件之间共享的类型 | `__tgt_bin_desc`、`__tgt_device_image`、`__tgt_target_table`、`KernelArgsTy` |
| [`include/OffloadEntry.h`](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OffloadEntry.h) | 运行时侧对单条 offload entry 的 C++ 包装 | `OffloadEntryTy`、`EntryTy` 字段 |

> 这三个文件之间的引用关系是：`omptarget.h` `#include "Shared/APITypes.h"`，`OffloadEntry.h` 同时 `#include` 二者。也就是说 `APITypes.h` 是最底层的共享类型层。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应学习目标的三条主线。

### 4.1 契约总览：编译器生成的 `__tgt_*` 调用与运行时入口

#### 4.1.1 概念说明

所谓“契约”，就是 Clang 在主机代码里**只允许调用** `include/omptarget.h` 中声明的那一组 `__tgt_*` 函数。只要 Clang 严格按照这些签名生成调用，而 libomptarget 严格按这些签名实现，二者就能解耦。

按功能，`__tgt_*` 函数族可以分为四大类：

1. **生命周期 / 注册类**：在程序启动时把镜像交给运行时、登记 `requires` 标志、初始化 RTL。
2. **数据搬运类（target data）**：对应 `#pragma omp target data` 与隐式数据映射的 `begin` / `end` / `update` 三个阶段。
3. **内核启动类**：对应 `#pragma omp target`（含 `teams` / `parallel` / `nowait`）真正在设备上执行一段代码。
4. **辅助类**：记录重放、RPC 回调注册、信息开关、设备信息打印等。

理解这套“动作清单”后，再看任何 OpenMP target 程序的底层，都能把每一行对应到其中一项。

#### 4.1.2 核心流程

从程序运行的时间线看，一次典型的卸载是这样串起来的：

```
程序启动
  │  Clang 在 .init_array 里生成构造函数：
  ├─► __tgt_register_requires(Flags)          // 登记 requires 子句
  ├─► __tgt_register_lib(&bin_desc)           // 把镜像描述符交给运行时
  │     运行时据此为每个兼容插件加载镜像，得到 __tgt_target_table
  │
进入 target data 区域
  ├─► __tgt_target_data_begin(DeviceId, ArgNum, ArgsBase, Args, ArgSizes, ArgTypes)
  │     // 按 ArgTypes(=tgt_map_type) 建立映射、按需 H2D 拷贝
  └─► ... 用户主机代码 ...
  └─► __tgt_target_data_end(...)              // 引用计数归零则 D2H 拷贝 + 解除映射

进入 target 区域（执行设备内核）
  └─► __tgt_target_kernel(Loc, DeviceId, NumTeams, ThreadLimit, HostPtr, KernelArgs*)
        // HostPtr 用来在镜像的入口表里找到对应 kernel；KernelArgs 携带参数与映射

程序退出
  └─► __tgt_unregister_lib(&bin_desc)         // 清理映射与设备资源
```

关键点：**主机指针 `HostPtr` 是“粘合剂”**——它既是主机侧某个 `target` 区域的地址，又等于镜像入口表里某条 `EntryTy` 的地址，运行时正是靠它把“主机这一处调用”对应到“设备镜像里那一个内核”。

#### 4.1.3 源码精读

先看注册 / 生命周期类入口，它们都是 `void` / `int` 的纯 C 函数，参数极少：

[include/omptarget.h:L328-L343](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L328-L343) 声明了 `__tgt_register_requires`（登记 `requires` 标志）、`__tgt_rtl_init` / `__tgt_rtl_deinit`（运行时初始化与去初始化）、`__tgt_register_lib`（把一个 `__tgt_bin_desc *` 描述符注册进运行时）、`__tgt_init_all_rtls`（一次性初始化全部插件）、`__tgt_unregister_lib`（注销镜像）。这一段是程序启动与退出时的“握手”入口。

[include/omptarget.h:L348-L349](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L348-L349) 是数据搬运“开始”阶段的签名。注意它接收的不是单个变量，而是**五个并行数组**：`ArgsBase`（变量基址）、`Args`（变量当前地址）、`ArgSizes`（每个变量字节数）、`ArgTypes`（每个变量的 `tgt_map_type` 标志）。`end` 与 `update` 的签名与之同构：

[include/omptarget.h:L370-L371](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L370-L371)（`__tgt_target_data_end`）与 [include/omptarget.h:L388-L390](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L388-L390)（`__tgt_target_data_update`）。三者只是“动作”不同（建表/拆表/单纯搬运），参数形状完全一致。

内核启动入口则引入了一个关键结构 `KernelArgsTy`：

[include/omptarget.h:L415-L416](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L415-L416) 的 `__tgt_target_kernel` 是“执行一段 target 区域”的核心入口，`HostPtr` 用于在入口表里定位内核，`KernelArgsTy *Args` 携带参数与映射信息。

`KernelArgsTy` 的定义在共享类型层，它是一次内核启动的“完整参数包”：

[include/Shared/APITypes.h:L91-L118](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L91-L118) 定义了 `KernelArgsTy`。要点：
- `Version`（结构体 ABI 版本号，保证 Clang 与运行时可演进而不破坏二进制兼容）；
- `NumArgs` + `ArgBasePtrs` / `ArgPtrs` / `ArgSizes` / `ArgTypes` / `ArgNames` / `ArgMappers`（与 `__tgt_target_data_*` 类似的并行参数数组）；
- `Tripcount`（`teams distribute` 循环的迭代数）；
- `Flags`（一个 64 位位域：`NoWait`、`IsCUDA`、`Cooperative`、`IsPtrArgs`、`StrictBlocksAndThreads` 等）；
- `UserNumBlocks[3]` / `UserThreadLimit[3]`（用户通过 `num_teams` / `thread_limit` 子句请求的三维块/线程数）；
- `DynCGroupMem`（请求的动态共享内存大小）。

> 小知识：紧随其后的 [include/Shared/APITypes.h:L119-L124](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L119-L124) 用两条 `static_assert` 锁死了 `KernelArgsTy` 与其 `Flags` 的字节大小。这正是“契约”的体现——结构体布局被冻结成 ABI。

返回值用 `OFFLOAD_SUCCESS` / `OFFLOAD_FAIL` 表示：

[include/omptarget.h:L31-L46](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L31-L46) 定义了成功/失败宏与 `__tgt_target_return_t` 枚举（`OMP_TGT_SUCCESS = 0`，`OMP_TGT_FAIL = ~0`）。

#### 4.1.4 代码实践

1. **目标**：把抽象的 `__tgt_*` 函数族落到一个可数的清单上。
2. **步骤**：在 [u1-l4](u1-l4-toolchain-and-run.md) 介绍的工具链基础上，把上面四大类各挑一个代表函数，填入下表（只读源码即可）：

   | 类别 | 代表函数 | omptarget.h 中的行号 |
   | --- | --- | --- |
   | 注册 / 生命周期 | `__tgt_register_lib` | L337 |
   | target data 开始 | ? | ? |
   | 内核启动 | ? | ? |
   | 记录重放 | `__tgt_target_kernel_replay`（见 L429） | ? |
3. **观察现象**：你会确认这些函数全部是 `extern "C"`、参数里反复出现 `ArgsBase / Args / ArgSizes / ArgTypes` 四元组。
4. **预期结果**：得到一张与运行时入口一一对应的“动作清单”。
5. 待本地验证：无需运行，纯源码阅读即可。

#### 4.1.5 小练习与答案

- **Q1**：`__tgt_target_data_begin` 与 `__tgt_target_data_update` 的参数形状几乎一样，它们语义上的区别是什么？
  - **答**：`begin` 负责进入数据区域——建立主机↔设备映射并按 `to` 触发 H2D 拷贝；`update` 不改变映射的引用计数，只按当前 `to`/`from` 做一次即时搬运。
- **Q2**：为什么 `KernelArgsTy` 第一个字段是 `Version`，并且后面跟着 `static_assert` 锁大小？
  - **答**：因为 Clang（生成者）与 libomptarget（消费者）是分开编译的，`Version` 与固定字节布局共同构成 ABI 保护，避免一方升级结构体时另一方读到错位的内存。

---

### 4.2 `tgt_map_type`：map 子句如何被编码为标志位

#### 4.2.1 概念说明

OpenMP 的 `map` 子句（`to` / `from` / `tofrom` / `alloc` / `release` / `delete`）以及修饰词（`always`、`close`、`present`）必须被压成“每个变量一个整数”，塞进 `ArgTypes` 数组传给运行时。这个整数的类型就是 `tgt_map_type`。

它是一个 **位掩码（bitmask）枚举**：不是“互斥取值”，而是“可按位或组合”。比如 `map(always tofrom:)` 会同时置上 `OMP_TGT_MAPTYPE_TO | OMP_TGT_MAPTYPE_FROM | OMP_TGT_MAPTYPE_ALWAYS`。

#### 4.2.2 核心流程

每个进入 `__tgt_target_data_*` 的变量，其 `ArgTypes[i]` 的解析流程是：

```
取出 ArgTypes[i] (uint64_t)
   │
   ├─ 用 (flags & TO) && (flags & ALWAYS)  → 决定是否“无视引用计数强制 H2D”
   ├─ 用 (flags & FROM)                    → 决定退出时是否 D2H
   ├─ 用 (flags & TARGET_PARAM)            → 把设备基址作为 kernel 参数传入
   ├─ 用 (flags & PRIVATE / LITERAL)       → 该变量不映射，按值/字面量处理
   ├─ 用 (flags & PTR_AND_OBJ)             → 指针与其所指对象都要处理
   ├─ 用 (flags & PRESENT)                 → 查不到映射就报错而非新建
   ├─ 用 (flags & MEMBER_OF 高16位)         → 这是结构体的第 N 个成员
   └─ 用 (flags & NON_CONTIG)              → 参考随附的 __tgt_target_non_contig 数组
```

引用计数（`ALWAYS`/`DELETE` 等）如何驱动真正的搬运时机，是 [u2-l4 数据映射](u2-l4-data-mapping.md) 的主题，本讲只建立“标志位 = 一组开关”的直觉。

#### 4.2.3 源码精读

[include/omptarget.h:L49-L91](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L49-L91) 定义了整个 `tgt_map_type` 枚举。为了便于查阅，按下表分组（值直接取自源码注释与定义）：

| 标志 | 值 | 含义 |
| --- | --- | --- |
| `OMP_TGT_MAPTYPE_NONE` | `0x000` | 无任何属性 |
| `OMP_TGT_MAPTYPE_TO` | `0x001` | 主机→设备拷贝 |
| `OMP_TGT_MAPTYPE_FROM` | `0x002` | 设备→主机拷贝 |
| `OMP_TGT_MAPTYPE_ALWAYS` | `0x004` | 无视引用计数强制拷贝 |
| `OMP_TGT_MAPTYPE_DELETE` | `0x008` | 强制解除映射 |
| `OMP_TGT_MAPTYPE_PTR_AND_OBJ` | `0x010` | 指针与其所指对象都处理 |
| `OMP_TGT_MAPTYPE_TARGET_PARAM` | `0x020` | 把设备基址作为 kernel 参数传入 |
| `OMP_TGT_MAPTYPE_RETURN_PARAM` | `0x040` | 返回映射数据的设备基址 |
| `OMP_TGT_MAPTYPE_PRIVATE` | `0x080` | 私有变量，不映射 |
| `OMP_TGT_MAPTYPE_LITERAL` | `0x100` | 按值拷贝，不映射 |
| `OMP_TGT_MAPTYPE_IMPLICIT` | `0x200` | 由编译器隐式补出的映射 |
| `OMP_TGT_MAPTYPE_CLOSE` | `0x400` | “close”修饰符 |
| `OMP_TGT_MAPTYPE_PRESENT` | `0x1000` | 查不到映射即运行时错误 |
| `OMP_TGT_MAPTYPE_OMPX_HOLD` | `0x2000` | OpenMP 扩展（OpenACC 兼容）的保持型引用计数 |
| `OMP_TGT_MAPTYPE_ATTACH` | `0x4000` | 处理完其它映射后再“挂接”指针，不改引用计数 |
| `OMP_TGT_MAPTYPE_FB_NULLIFY` | `0x8000` | 查不到时回退成 `null` 而非保留原指针 |
| `OMP_TGT_MAPTYPE_NON_CONTIG` | `0x100000000000` | 非连续 target-update 描述符 |
| `OMP_TGT_MAPTYPE_MEMBER_OF` | `0xffff000000000000` | 结构体成员标记，成员序号编码在高 16 位 |

注意三处细节：
- **低 16 位（`0x1`–`0x8000`）是“行为开关”**，可自由组合。
- **`MEMBER_OF` 用高 16 位编码“我是结构体的第几个成员”**——这是一个把信息塞进整数的高位、用掩码取出的常见手法：成员序号 = `(flags >> 48) & 0xffff`，再减 1。
- **`NON_CONTIG` 与 `MEMBER_OF` 用到了 64 位的高位**，这也是为什么 `tgt_map_type` 被声明为 `enum tgt_map_type : uint64_t`（而非默认 `int`）。

非连续更新的几何信息由另一结构体承载：

[include/omptarget.h:L267-L271](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L267-L271) 定义 `__tgt_target_non_contig`（`Offset` / `Count` / `Stride`），用于数组切片式映射。

#### 4.2.4 代码实践

1. **目标**：把 `map` 子句与位标志对应起来。
2. **步骤**：写出下面三段代码，人工推算每个变量应当得到的 `ArgTypes` 值（按位或）：

   ```c
   // (a)
   #pragma omp target data map(to: a[0:N])
   // (b)
   #pragma omp target data map(always tofrom: b)
   // (c)
   #pragma omp target map(to: arr) present(p)
   ```
3. **观察现象**：
   - (a) 应至少包含 `OMP_TGT_MAPTYPE_TO`；
   - (b) 应包含 `TO | FROM | ALWAYS`；
   - (c) 中 `p` 应额外置 `PRESENT`。
4. **预期结果**：能用 16 进制写出每个变量的标志位组合（例如 (b) = `0x007`）。
5. 待本地验证：真实编码还可能叠加 `TARGET_PARAM` / `IMPLICIT` 等位，可在下一节实践中用 IR 实际核对。

#### 4.2.5 小练习与答案

- **Q1**：`OMP_TGT_MAPTYPE_MEMBER_OF` 为什么占据高 16 位？
  - **答**：因为它不仅要表示“我是结构体成员”，还要顺便编码“我是第几个成员”。把序号塞进高 16 位，低 48 位留给行为开关，互不干扰。
- **Q2**：`OMP_TGT_MAPTYPE_PRESENT` 与 `OMP_TGT_MAPTYPE_DELETE` 都会“动引用计数”，方向有何不同？
  - **答**：`PRESENT` 不改引用计数，只是要求该变量必须**已经**存在映射，否则报错；`DELETE` 则是**强制**解除映射（哪怕引用计数还没归零）。

---

### 4.3 二进制描述符与 offload entries

#### 4.3.1 概念说明

回到本讲开头那张“两张表 + 一段镜像字节”的直觉，本模块把它们具象为三个结构体：

- **一段镜像字节** → `__tgt_device_image`：记录“这块设备代码从哪到哪、以及它的入口表范围”。
- **整个程序的全部镜像** → `__tgt_bin_desc`（binary descriptor）：把“所有设备镜像 + 主机入口表”打包，是程序启动时交给运行时的总账本。
- **设备侧可见的入口表** → `__tgt_target_table`：镜像被插件加载后，运行时回填这张表，表示“设备上现在能看到哪些入口”。

而“表里的一条记录”就是 **offload entry**，其 C 层类型是 `llvm::offloading::EntryTy`（定义在 monorepo 的 `llvm/Frontend/Offloading/Utility.h`，**不在本子项目内**），运行时侧再用 `OffloadEntryTy` 包装它。

#### 4.3.2 核心流程

从“源码里写 `#pragma omp target`”到“运行时拿到镜像”，流程是：

```
Clang 代码生成
  ├─ 为每个 target 区域 / declare target 全局 生成一条 EntryTy（含地址、大小、符号名、标志）
  ├─ 为每个目标三元组生成一段设备镜像 + 该镜像专属的 EntryTy 表
  └─ 汇总成一个 __tgt_bin_desc：
        { NumDeviceImages, DeviceImages[], HostEntries[Begin,End) }

程序启动（构造函数）
  └─ __tgt_register_lib(&bin_desc)
        运行时遍历 DeviceImages[]，为每张镜像找兼容插件并加载
        加载成功后得到 __tgt_target_table（指向设备可见的 entries）

target 区域执行
  └─ __tgt_target_kernel(..., HostPtr, ...)
        用 HostPtr 在 table 的 entries 中匹配，定位到设备内核
```

注意一个层次关系：`__tgt_bin_desc` 是**主机侧**的总账本（程序启动时由 Clang 生成的构造函数填好）；`__tgt_target_table` 是**设备侧**的视图（运行时加载镜像后才存在）。

#### 4.3.3 源码精读

先看“一段镜像字节”：

[include/Shared/APITypes.h:L30-L36](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L30-L36) 定义 `__tgt_device_image`：`ImageStart` / `ImageEnd` 是设备代码字节范围的起止指针（两个指针相减即镜像大小），`EntriesBegin` / `EntriesEnd` 是该镜像专属入口表的半开区间，元素类型是 `llvm::offloading::EntryTy`。

再看“整个程序的总账本”：

[include/Shared/APITypes.h:L46-L52](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L46-L52) 定义 `__tgt_bin_desc`：`NumDeviceImages` 说明支持几种设备类型；`DeviceImages` 是一个数组，每种设备类型一张 `__tgt_device_image`；`HostEntriesBegin` / `HostEntriesEnd` 是主机侧入口表（与镜像入口表分开存放）。

最后看“设备侧可见的入口表”：

[include/Shared/APITypes.h:L55-L60](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L55-L60) 定义 `__tgt_target_table`：结构与镜像里的入口区间同构，但语义是“这张镜像在某个具体设备上加载后，设备能看到哪些入口”，由插件回填。

> 补充：紧邻的 [include/Shared/APITypes.h:L63-L65](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L63-L65) 定义 `__tgt_device_binary`（一个不透明 `handle`），是插件加载镜像后返回给上层的句柄；[include/Shared/APITypes.h:L38-L42](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/Shared/APITypes.h#L38-L42) 的 `__tgt_device_info` 携带 `Context` / `Device` / `Platform` 三个不透明指针，供需要直接拿设备句柄的场景使用。

现在看“表里的一条记录”。`EntryTy` 本身定义在 monorepo 的 `llvm/Frontend/Offloading/Utility.h`（不在本子项目内，故不给出行号链接），但从本子项目的 `OffloadEntryTy` 包装类可以精确推断出它有哪些字段：

[include/OffloadEntry.h:L23-L45](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OffloadEntry.h#L23-L45) 定义 `OffloadEntryTy`，它持有一个 `llvm::offloading::EntryTy &` 引用。通过其内联方法可读出 `EntryTy` 的全部对外字段：

- `OffloadEntry.Address`（见 `getnAddress()`，L35）——该条目的地址（在主机侧即 `target` 区域或全局的地址，用于和 `HostPtr` 匹配）；
- `OffloadEntry.Size`（见 `getSize()`，L33）——条目大小，**非 0 表示这是一个全局变量**（见 `isGlobal()`，L32）；
- `OffloadEntry.SymbolName`（见 `getName()` / `getNameAsCStr()`，L36-L37）——符号名（`llvm::StringRef`）；
- `OffloadEntry.Flags`（见 `hasFlags()`，L42-L44）——承载 `OpenMPOffloadingDeclareTargetFlags`（如 `OMP_DECLARE_TARGET_LINK`、`OMP_DECLARE_TARGET_INDIRECT`），见 [include/omptarget.h:L94-L103](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/omptarget.h#L94-L103)。

`OffloadEntryTy` 还能反查所属镜像：[include/OffloadEntry.h:L38](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OffloadEntry.h#L38) 的 `getBinaryDescription()` 返回所属的 `__tgt_bin_desc *`，这正是把“一条入口”关联回“总账本”的桥梁。

> 关于 `EntryTy` 的准确性说明：其权威字段定义在 `llvm-project` monorepo 的 `llvm/Frontend/Offloading/Utility.h`（编译器前端与运行时共享），本讲据 `OffloadEntry.h` 的可观察用法列出 `Address / Size / SymbolName / Flags` 四个字段；如需完整定义（含 `Name` / `Data` 等历史字段），请到该 monorepo 头文件确认——**待确认**。

#### 4.3.4 代码实践

1. **目标**：在脑中画出 `__tgt_bin_desc → __tgt_device_image → EntryTy` 的包含关系。
2. **步骤**：对照上面三段源码，用伪结构体写出“一个含 2 种设备镜像、每张镜像 3 个入口、主机侧 1 个入口”的 `bin_desc`，标出每个字段的来源行号。
3. **观察现象**：你会清楚地看到 entries 同时存在于“镜像内”和“主机侧”两处，二者用不同字段分别给出。
4. **预期结果**：得到一张嵌套结构图，能回答“`NumDeviceImages` 从哪来、`HostEntriesBegin` 指向什么”。
5. 待本地验证：纯结构梳理，无需运行。

#### 4.3.5 小练习与答案

- **Q1**：`__tgt_device_image` 为什么用 `ImageStart` / `ImageEnd` 两个指针而不是 `(ptr, size)`？
  - **答**：两个指针构成半开区间，遍历时 `begin != end` 即可终止，且与 C++ 迭代器语义一致；同时这两个指针就是链接器排放的符号地址，天然可用。
- **Q2**：`OffloadEntryTy::isGlobal()` 用 `Size != 0` 判断，这说明了什么？
  - **答**：`target` 区域（函数）的大小为 0，而 `declare target` 全局变量有实际大小；因此 `Size` 是否为 0 正好区分“可调用内核”与“可读写全局变量”。
- **Q3**：`__tgt_target_table` 与 `__tgt_device_image` 里的 entries 区间，内容上有何不同？
  - **答**：前者是镜像**加载到设备后**、设备实际可见的入口视图（由插件回填）；后者是镜像**自带的**入口元信息区间。运行时靠前者把 `HostPtr` 匹配到设备内核。

---

## 5. 综合实践

把三个模块串起来，完成本讲指定的实践：**查看一段真实 OpenMP target 程序生成的 `__tgt_*` 调用，并逐字段解释其中的 `__tgt_bin_desc`。**

**目标程序**（示例代码，需自行创建为 `t.c`）：

```c
// 示例代码：需自行保存为 t.c
#include <stdio.h>
int main(void) {
  int data[4] = {1, 2, 3, 4}, sum = 0;
  #pragma omp target data map(to: data) map(from: sum)
  {
    #pragma omp target map(tofrom: sum)
    for (int i = 0; i < 4; ++i) sum += data[i];
  }
  printf("sum=%d\n", sum);
  return 0;
}
```

**操作步骤**（在 [u1-l2](u1-l2-build-system.md) / [u1-l4](u1-l4-toolchain-and-run.md) 已配置好 clang 的前提下）：

1. 编译到 LLVM IR，便于查看主机侧对运行时的调用：

   ```bash
   clang -fopenmp -fopenmp-targets=x86_64-pc-linux-gnu -O0 -S -emit-llvm t.c -o t.ll
   ```

   （`x86_64-pc-linux-gnu` 对应 host 插件，无 GPU 也可学习。）
2. 在 `t.ll` 中检索契约符号：

   ```bash
   grep -nE '__tgt_(register_lib|register_requires|target_data_begin|target_data_end|target_kernel)' t.ll
   ```
3. 逐字段解释 `__tgt_bin_desc`。在 IR 里 Clang 会生成一个全局的 `bin_desc` 并在某构造函数中调用 `__tgt_register_lib`。请对照本讲 4.3 的字段表，回答：
   - 该程序的 `NumDeviceImages` 是几？（应为 1，只用了 host 三元组）
   - `DeviceImages[0].ImageStart` / `ImageEnd` 指向哪段符号？（指向设备镜像字节区间）
   - `DeviceImages[0].EntriesBegin` / `EntriesEnd` 之间有几条 `EntryTy`？（应为 1 条：那个 `target` 内核）
   - `HostEntriesBegin` / `HostEntriesEnd` 指向什么？

**需要观察的现象**：
- 每个 `target data` / `target` 都能找到一个对应的 `__tgt_target_data_*` / `__tgt_target_kernel` 调用；
- `__tgt_target_data_begin` 的 `ArgTypes` 参数是一个常量数组，其元素就是 4.2 推算出的位标志组合；
- 存在一个构造函数在程序启动时调用 `__tgt_register_lib(&bin_desc)`。

**预期结果**：你能把 IR 里每一处 `__tgt_*` 调用与源码每一行 `#pragma omp` 逐一对应，并填出 `__tgt_bin_desc` 的每个字段含义。

**待本地验证**：上述命令的行为依赖你本机的 clang 版本与 host 插件是否启用；若 `grep` 无结果，请确认 clang 启用了 OpenMP 卸载（`-fopenmp-targets`）。

## 6. 本讲小结

- **契约就是头文件**：Clang 与 libomptarget 之间靠 `include/omptarget.h` 与 `include/Shared/APITypes.h` 里的一组 `extern "C"` 结构体与 `__tgt_*` 函数签名对接，二者可独立演进。
- **`__tgt_*` 分四类**：注册/生命周期（`__tgt_register_lib` 等）、数据搬运（`__tgt_target_data_begin/end/update`）、内核启动（`__tgt_target_kernel`）、辅助（记录重放 / RPC / 信息开关）。
- **map 子句 = 位掩码**：`tgt_map_type` 把 `to/from/always/present/...` 压进一个 `uint64_t`，低 16 位是行为开关，高 16 位编码结构体成员序号。
- **三件套描述镜像**：`__tgt_device_image`（一段镜像 + 入口区间）、`__tgt_bin_desc`（全部镜像的总账本）、`__tgt_target_table`（设备加载后的入口视图）层层嵌套。
- **EntryTy 是入口表的“一行”**：含 `Address`（与 `HostPtr` 匹配）、`Size`（区分内核/全局）、`SymbolName`、`Flags`；运行时侧由 `OffloadEntryTy` 包装。
- **`HostPtr` 是粘合剂**：它把主机侧某处 `target` 调用与设备镜像里的某个内核精确对应起来。

## 7. 下一步学习建议

本讲只建立了“数据结构与契约签名”的静态地图，**还没讲运行时如何实现这些入口**。建议：

1. 接着学 [u2-l1 运行时初始化与库注册入口](u2-l1-runtime-entry.md)，看 `__tgt_register_lib` / `__tgt_rtl_init` 在 `libomptarget/interface.cpp` 里的真实实现，以及它如何经由 `PluginManager` 把 `__tgt_bin_desc` 分发给各插件。
2. 在进入第二单元前，可先读本讲的三个头文件原文（它们都很短），把本讲的表格与源码逐行对一遍，建立肌肉记忆。
3. 关于 `tgt_map_type` 如何真正驱动数据搬运与引用计数，留到 [u2-l4 数据映射机制](u2-l4-data-mapping.md) 与 [u2-l5 target data 流程](u2-l5-target-data-flow.md) 深入。
