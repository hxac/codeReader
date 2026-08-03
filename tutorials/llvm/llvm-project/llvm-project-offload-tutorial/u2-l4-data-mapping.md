# 主机-设备数据映射机制

## 1. 本讲目标

OpenMP 目标卸载的 `map` 子句（`to`/`from`/`tofrom`/`alloc`）在运行时到底是怎么变成「设备指针」的？同一个主机变量被多个 `target data` 区域嵌套引用时，运行时如何知道「该复制了」「该释放了」？本讲解答这些问题。

学完后你应当掌握：

1. `HostDataToTargetTy` 这条「映射条目」的内部结构，以及它为什么用 **动态（Dyn）+ 保持（Hold）双引用计数** 来决定数据何时复制与何时释放。
2. 主机指针如何在一张有序映射表 `HDTTMap` 中被查找到（含「包含 / 向前延伸 / 向后延伸」三种命中情形），以及这张表如何被互斥锁安全地并发访问。
3. `getTargetPointer`（enter 路径，建立映射并 H2D 搬运）与 `getTgtPtrBegin`（exit 路径，递减引用计数并延迟删除）两条核心算法。
4. 影子指针（`ShadowPtrInfoTy`）、事件（`Event`）与 `MappingConfig` 在映射机制中扮演的辅助角色。

本讲聚焦的数据结构属于 libomptarget 的 OpenMP 上层逻辑层，位于 `include/OpenMP/`（契约/声明）与 `libomptarget/OpenMP/`（实现）两个最小模块。它建立在 [u2-l3](u2-l3-device-abstraction.md) 讲过的 `DeviceTy` 之上——`DeviceTy` 内嵌一个 `MappingInfoTy`，本讲就是把这个内嵌对象拆开讲透。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**直觉一：为什么需要「映射表」？** 主机和设备的地址空间通常是分离的。主机指针 `&A[0]` 在设备上毫无意义，必须有一个「翻译表」记录：主机区间 `[HstPtrBegin, HstPtrEnd)` 对应设备区间 `[TgtPtrBegin, ...)`。运行时每次遇到 `map` 子句，第一件事就是查这张表，查到就直接用设备指针，查不到就在设备上分配并登记。

**直觉二：为什么需要「引用计数」？** OpenMP 允许嵌套：

```c
#pragma omp target data map(tofrom: A)   // 外层
{
  #pragma omp target data map(tofrom: A) // 内层
  { ... }
}
```

外层进入时分配、内层进入时不应再分配、内层退出时不应释放、外层退出时才真正释放。这跟智能指针的 `shared_ptr` 完全一样：每进入一次引用计数 +1，每退出一次 -1，减到 0 才真正回收设备内存。运行时用两个引用计数器：**动态引用计数（DynRefCount，OpenMP 4.5 标准）** 和 **保持引用计数（HoldRefCount，为 OpenACC 扩展的 `ompx_hold`）**。两者之和即总引用计数。

**直觉三：「复制」与「释放」的触发时机不同。** 进入区域的复制（H2D）取决于 `to` 标志和「是不是新条目 / `always`」；退出区域的释放取决于引用计数是否归零（`IsLast`）。二者是正交的判断，分别由 `getTargetPointer` 和 `getTgtPtrBegin` 负责。

如果你对 `tgt_map_type` 位标志（`OMP_TGT_MAPTYPE_TO` 等）还不熟，请先复习 [u1-l5](u1-l5-compiler-runtime-contract.md)；它们是本讲反复用到的输入。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `include/OpenMP/Mapping.h` | 全部映射数据结构的声明：`MappingConfig`、`ShadowPtrInfoTy`、`HostDataToTargetTy`、`HDTTMap` 键与表、`TargetPointerResultTy`、`LookupResult`、`MappingInfoTy`。 |
| `libomptarget/OpenMP/Mapping.cpp` | 上述结构的实现，重点是 `lookupMapping`、`getTargetPointer`、`getTgtPtrBegin`、`eraseMapEntry`、`deallocTgtPtrAndEntry`。 |
| `include/ExclusiveAccess.h` | `ProtectedObj` + `Accessor` 模板，提供 `HDTTMap` 的互斥访问包装（RAII 锁）。 |
| `libomptarget/device.cpp` | `HostDataToTargetTy::addEventIfNecessary` 的实现，以及 `DeviceTy` 构造时如何内嵌 `MappingInfo`。 |
| `libomptarget/omptarget.cpp` | `targetDataBegin` / `targetDataEnd` 调用 `getTargetPointer` / `getTgtPtrBegin` 的上层语境，以及真正调用 `eraseMapEntry`+`deallocTgtPtrAndEntry` 完成回收的代码。 |
| `include/omptarget.h` | `OMP_TGT_MAPTYPE_*` 位标志定义（u1-l5 已讲，本讲引用）。 |

**分层关系**：`MappingInfoTy` 是纯逻辑层，它不直接碰硬件，所有设备分配/搬运都委托给 `DeviceTy`（如 `Device.allocData`、`Device.submitData`、`Device.deleteData`）。本讲的算法（何时查表、何时建映射、何时搬数据、何时释放）全部落在 `MappingInfoTy` 里，`DeviceTy` 只是它的「手脚」。

## 4. 核心概念与源码讲解

### 4.1 映射条目 HostDataToTargetTy：双引用计数状态机

#### 4.1.1 概念说明

`HostDataToTargetTy` 是映射表里的**一条记录**，描述「一段主机内存 ↔ 一段设备内存」的绑定关系，外加这段绑定的「活跃程度」（引用计数）。它是整个映射机制的原子单元。

它同时承载两类信息：
- **不可变的绑定信息**（`const` 成员）：主机区间的 `HstPtrBase/HstPtrBegin/HstPtrEnd`、设备分配起点 `TgtAllocBegin`、设备映射起点 `TgtPtrBegin`。
- **可变的状态信息**（封装在 `StatesTy` 里）：两个引用计数、影子指针集合、事件、以及退出时的线程计数器。

为什么把可变状态单独包进 `std::unique_ptr<StatesTy> States`？因为映射表用 `std::set` 存储，而 `std::set` 的迭代器是 `const` 的——要在一个 `const` 迭代器指向的元素上修改引用计数，就得用指针间接持有一份可变状态。

#### 4.1.2 核心流程

引用计数的状态机可以这样概括（以动态计数 `DynRefCount` 为例，`HoldRefCount` 对称）：

```
新建条目 ──> DynRef = 1
   │  每次 enter（getTargetPointer，IsContained）
   ▼  incRefCount():  DynRef++ （除非已是 INF）
  DynRef = N
   │  每次 exit（getTgtPtrBegin）
   ▼  decRefCount():  DynRef-- （除非 INF 或 0），返回 total
  ... 直到 DynRef 归 0 且 HoldRef 也为 0
   │  decShouldRemove() == true  ──> 标记 IsLast ──> 延迟删除
```

有几个特殊值需要记住：

- **INF（无穷）**：值为 `~(uint64_t)0`，表示「永不释放」。`omp_target_associate_ptr` 显式关联的指针（见 4.1.3 的 `associatePtr`）和 `omp declare target link` 全局变量都用它。INF 不参与加减、不会被删除。
- **reset**：`resetRefCount` 把计数器重置为 1（除非 INF），专为 `target exit data` 的 `delete` 子句服务——它让紧接着的一次 `decRefCount` 一定能把计数压到 0。
- **总计数**：`getTotalRefCount()` 在任一计数为 INF 时直接返回 INF，否则返回两者之和。

「这次递减之后条目是否该删除」是关键判断，由 `decShouldRemove` 给出：

\[ \text{decShouldRemove} = (\text{OtherRefCount} = 0)\ \wedge\ \begin{cases}\text{ThisRefCount} \ne \text{INF} & \text{若 AfterReset}\\ \text{ThisRefCount} = 1 & \text{否则}\end{cases} \]

含义：**只有当另一个计数器已经是 0，且本计数器再减一次就归零时，这次退出才该删除条目**。`OtherRefCount > 0` 时永远返回 false——这正是「嵌套区域里层退出不释放」的依据。

#### 4.1.3 源码精读

**条目结构与构造**（构造函数决定了新条目的初始计数）：

[include/OpenMP/Mapping.h:114-184](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L114-L184) —— `HostDataToTargetTy` 的字段与构造函数。注意构造函数对 `StatesTy` 的初始化：`UseHoldRefCount` 决定把初始值 1 放进哪个计数器，`IsINF` 决定是否用 INF。普通新条目（`UseHoldRefCount=false, IsINF=false`）得到 `DynRef=1, HoldRef=0`。

```cpp
States(std::make_unique<StatesTy>(UseHoldRefCount ? 0
                                  : IsINF         ? INFRefCount
                                                  : 1,
                                  !UseHoldRefCount ? 0
                                  : IsINF          ? INFRefCount
                                                   : 1))
```

**状态容器 `StatesTy`**：

[include/OpenMP/Mapping.h:129-167](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L129-L167) —— 把两个计数器、影子指针集合、事件、退出线程计数器封装在一起，注释解释了为什么需要双计数器（动态计数是 OpenMP 4.5 标准，保持计数是 OpenACC 扩展）。

**计数器操作三件套**：

[include/OpenMP/Mapping.h:234-258](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L234-L258) —— `incRefCount`（非 INF 才加，带溢出断言）与 `decRefCount`（非 INF 且 >0 才减，返回总计数）。

[include/OpenMP/Mapping.h:275-285](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L275-L285) —— `decShouldRemove`，即上面的公式，决定「这次递减是否该触发删除」。

**显式关联用 INF**：

[libomptarget/OpenMP/Mapping.cpp:51-100](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L51-L100) —— `associatePtr`（对应 `omp_target_associate_ptr`）建条目时传 `IsRefCountINF=true`，于是动态计数为 INF，运行时永不自动释放它，只能由 `disassociatePtr` 主动解除。

#### 4.1.4 代码实践

**实践目标**：亲眼看到引用计数的字符串化与 INF 的特殊处理。

**操作步骤**：

1. 打开 [include/OpenMP/Mapping.h:124-127](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L124-L127)，阅读 `INFRefCount` 常量与 `refCountToStr`。
2. 在源码阅读型实践中，对照 `getTotalRefCount`（[Mapping.h:189-194](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L189-L194)）手算几个组合：
   - `DynRef=2, HoldRef=0` → 总数 2
   - `DynRef=INF, HoldRef=3` → 总数 INF（注意不是 INF+3）
   - `DynRef=0, HoldRef=0` → 总数 0（这正是删除条件）

**需要观察的现象 / 预期结果**：INF 的优先级最高，只要任一计数器是 INF，总数就是 INF。这保证了显式关联的指针不会被错误地「总数归零」而删除。

#### 4.1.5 小练习与答案

**练习 1**：一个条目 `DynRef=1, HoldRef=1`，现在 `target exit data` 对它做一次普通 `decRefCount`（动态）。它会触发删除吗？

**答案**：不会。`decShouldRemove(UseHoldRefCount=false)` 中 `OtherRefCount = HoldRefCount = 1 > 0`，直接返回 false。动态计数减成 0，但保持计数还撑着，条目继续存活。

**练习 2**：`resetRefCount` 为什么重置为 1 而不是 0？

**答案**：因为 `delete` 子句的语义是「让紧接着的一次递减把它压到 0」。重置为 1 后，随后的 `decRefCount` 减到 0、`decShouldRemove` 返回 true，从而触发延迟删除。若重置为 0，`decRefCount` 因 `>0` 判断不成立而不动作，删除路径会出问题。

### 4.2 HDTTMap 映射表与并发访问保护

#### 4.2.1 概念说明

每个设备有一张映射表 `HDTTMap`，它是 `std::set<HostDataToTargetMapKeyTy>`，**按主机指针起始地址 `HstPtrBegin` 排序**。有序是为了 O(log n) 的区间查找（4.3 讲）。这张表是多线程共享的——一个线程可能在 enter 时插入条目，另一个线程同时在 exit 时查找/删除——因此必须互斥。

互斥通过两层完成：
1. **表级锁**：`ProtectedObj<HostDataToTargetListTy>` 把表和一把 `std::mutex` 绑在一起，只能通过 `getExclusiveAccessor()` 拿到一个 RAII 的 `Accessor` 才能访问表。
2. **条目级锁**：每条 `HostDataToTargetTy` 自带一把 `Mtx`，允许「释放表锁后继续安全地操作某一条目」（因为 `TargetPointerResultTy` 在持有期间会锁住条目）。

这种「表锁 + 条目锁」分层是为了缩小临界区：查表需要表锁，但搬数据、改引用计数这些耗时操作可以在只持有条目锁的情况下进行，从而提高并发度。

#### 4.2.2 核心流程

```
线程 A 要查/改映射表：
  1. HDTTMapAccessorTy acc = HostDataToTargetMap.getExclusiveAccessor();  // 锁表
  2. 用 acc->find / acc->emplace / acc->erase 操作表
  3. 把命中的条目「搬进」TargetPointerResultTy（TPR 构造时锁条目）
  4. 可选：acc.destroy() 提前释放表锁（临界区结束），但条目锁还在
  5. TPR 析构时解锁条目
```

`Accessor` 是 RAII：构造时 `lock()`，析构时 `unlock()`，`destroy()` 可提前显式释放。`getExclusiveAccessor(true)` 则返回一个不持锁的空 accessor（用于条件性地不需要访问时）。

#### 4.2.3 源码精读

**表与访问器类型**：

[include/OpenMP/Mapping.h:636-650](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L636-L650) —— `MappingInfoTy` 把 `HostDataToTargetMap` 声明为 `ProtectedObj<HostDataToTargetListTy>`，并定义别名 `HDTTMapAccessorTy`。注释说明用「包装键间接」是为了在不让底层条目失效的前提下并发修改集合。

**ProtectedObj / Accessor 的互斥实现**：

[include/ExclusiveAccess.h:26-99](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/ExclusiveAccess.h#L26-L99) —— `ProtectedObj` 持有对象 + 互斥量；`Accessor` 的 `operator->/operator*` 透明转发到内部对象，`destroy()` 释放锁并把指针置空。

**条目级锁与 TPR 的所有权语义**：

[include/OpenMP/Mapping.h:372-441](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L372-L441) —— `TargetPointerResultTy`（TPR）。关键设计：只要 TPR 持有非空 `Entry`，该条目就被锁住；TPR 析构或 `reset()` 时解锁。`Flags` 是位域，记录 `IsNewEntry/IsHostPointer/IsPresent/IsLast/IsContained`。注释明确：「一个非空 Entry 的 TPR 拥有该条目，只要 TPR 存在条目就被锁」。

[include/OpenMP/Mapping.h:327-337](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L327-L337) —— `HostDataToTargetTy::lock()/unlock()` 与其私有 `Mtx`，注释强调「必须先持有 HDTTMap 锁，再尝试锁条目」——这规定了**锁序**，是避免死锁的纪律。

**dump 工具：把整张表打出来**：

[libomptarget/OpenMP/Mapping.cpp:21-49](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L21-L49) —— `dumpTargetPointerMappings` 先拿独占访问器，再遍历打印每条的 Host/Target 指针、大小、Dyn/Hold 计数。这正是运行时在出错或开 `LIBOMPTARGET_INFO` 时打印映射表的实现。

#### 4.2.4 代码实践

**实践目标**：理解「表锁 + 条目锁」如何让临界区最小化。

**操作步骤**：

1. 阅读 [Mapping.cpp:207-218](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L207-L218) 中 `getTargetPointer` 开头：它接收外部已经持有的 `HDTTMapAccessorTy &HDTTMap`（调用方先锁表）。
2. 注意函数末尾 [Mapping.cpp:329-330](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L329-L330)：`if (ReleaseHDTTMap) HDTTMap.destroy();`——在条目已被 TPR 锁住之后，**主动释放表锁**，后续的 `submitData`（耗时 H2D 搬运）只持条目锁执行。

**需要观察的现象 / 预期结果**：你会看到「先建映射条目并把它锁进 TPR → 再放表锁 → 最后才搬数据」的顺序。这就是并发设计的精髓：把耗时的数据搬运移出表锁的临界区。

#### 4.2.5 小练习与答案

**练习**：为什么锁序必须是「先表锁，后条目锁」，反过来会怎样？

**答案**：反过来会死锁。若线程 A 先锁条目 X 再请求表锁，线程 B 先锁表再请求条目 X 的锁，二者会循环等待。规定统一锁序后，任何线程都是「表 → 条目」，不会形成等待环。源码注释 [Mapping.h:335-337](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L335-L337) 明确了这条纪律。

### 4.3 lookupMapping：区间查找与 LookupResult

#### 4.3.1 概念说明

`map` 子句给出的往往不是「整个变量」，而是「数组片段」，例如 `A[10:20]`（从下标 10 起 20 个元素）。运行时要在有序表里判断：这段 `[HP, HP+Size)` 与已登记的某条区间是什么关系？可能的结果有三种：

- **IsContained（被包含）**：`[HP, HP+Size)` 完全落在某条已登记区间内 → 直接复用，按偏移算出设备指针。
- **ExtendsBefore（向前延伸）**：片段起点在已登记区间之前，但尾部伸进了已登记区间 → 通常是警告（显式时不允许，隐式时按包含处理）。
- **ExtendsAfter（向后延伸）**：片段起点在已登记区间内，但尾部超出 → 同样警告。

`LookupResult` 把这三种标志连同命中的条目（包在 TPR 里）一起返回。

#### 4.3.2 核心流程

利用 `std::set` 的有序性，用 `upper_bound(HP)` 找到「第一个 `HstPtrBegin > HP` 的元素」，然后只看它的**前驱** `std::prev(Upper)`（因为前驱的 `HstPtrBegin <= HP`），判断 `HP` 是否落在 `[HstPtrBegin, HstPtrEnd)` 内：

```
Upper = upper_bound(HP)              # 第一个起点 > HP 的条目
Cand  = std::prev(Upper)             # 候选：起点 <= HP 的最近条目
if Cand 存在:
    IsContained   = (HP >= Cand.HstPtrBegin) 且 (HP < Cand.HstPtrEnd)
                  且 (Size>0 时还要求 HP+Size <= Cand.HstPtrEnd)
    ExtendsAfter  = 片段尾部超出 Cand.HstPtrEnd
if 既不包含也不后延, 再看 Upper 本身:
    ExtendsBefore = 片段尾部 > Upper.HstPtrBegin（片段伸进了下一条）
```

`Size == 0` 是特例（只查一个点，常用于基指针查询），判断更宽松。

#### 4.3.3 源码精读

**LookupResult 结构**：

[include/OpenMP/Mapping.h:443-453](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L443-L453) —— 三个位标志 `IsContained/ExtendsBefore/ExtendsAfter`，内嵌一个 `TargetPointerResultTy TPR`。

**查找实现**：

[libomptarget/OpenMP/Mapping.cpp:138-205](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L138-L205) —— `lookupMapping`。注意 `Size==0` 分支（[L155-171](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L155-L171)）还有一个「扩展基指针」的特殊处理：当 `HP` 不在 `[HstPtrBegin, HstPtrEnd)` 内、却落在 `[HstPtrBase, HstPtrBegin)` 内时，仍把它视作命中——这是为了支持「用结构体基指针查询成员」的情形。`Size>0` 分支（[L172-202](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L172-L202)）则是标准的包含/延伸判断。

**有序键的设计**：

[include/OpenMP/Mapping.h:345-365](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L345-L365) —— `HostDataToTargetMapKeyTy` 把键值 `KeyValue`（即 `HstPtrBegin`）显式存一份，并提供 `operator<` 与裸 `uintptr_t` 的比较重载，让 `std::set::find/upper_bound` 能直接用整数查询而无需先构造条目——这是上面 `upper_bound(HP)` 能成立的基础（透明比较，C++ 异构查找）。

#### 4.3.4 代码实践

**实践目标**：用具体地址手算一次 `lookupMapping` 的结果。

**操作步骤**：

1. 假设表里只有一条：`HstPtrBase=0x1000, HstPtrBegin=0x1000, HstPtrEnd=0x1100`（即 256 字节的变量 `A`，已 map）。
2. 手算下列查询的 `LookupResult.Flags`：
   - 查 `HP=0x1040, Size=32`（`A[64:8]`，假设 int）
   - 查 `HP=0x10F0, Size=32`（尾部超出）
   - 查 `HP=0x0F80, Size=0x100`（向前延伸进 A）
   - 查 `HP=0x2000, Size=16`（完全不沾边）

**预期结果**：
- 第 1 个：`IsContained=true`（0x1040+0x20=0x1060 <= 0x1100）→ 复用，设备指针 = `TgtPtrBegin + 0x40`。
- 第 2 个：`ExtendsAfter=true`（0x10F0 在内，但 0x10F0+0x20=0x1110 > 0x1100）。
- 第 3 个：`ExtendsBefore=true`（0x0F80+0x100=0x1080 > 0x1000，伸进了 A）。
- 第 4 个：三个标志全 false，`TPR.Entry` 为空 → 视作「不存在」。

#### 4.3.5 小练习与答案

**练习**：为什么 `Size==0` 的查询允许命中 `HstPtrBase` 而非 `HstPtrBegin`？

**答案**：`HstPtrBase` 是结构体的基地址，可能小于 `HstPtrBegin`（某个成员的起始）。当用基指针（而非成员指针）查询、且只关心「这个基指针对应的设备地址」时，`Size==0` 的宽松命中能让 `map(p[:])` 这类指向结构体内部指针的 attach 操作正确找到所属条目。见 [Mapping.cpp:164-171](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L164-L171) 的注释。

### 4.4 getTargetPointer：建立映射与 H2D 数据搬运（enter 路径）

#### 4.4.1 概念说明

`getTargetPointer` 是 **enter 路径**（`target data` / `target enter data` / `target` 区域进入）的核心。它对每个 `map` 条目做四件事：

1. **查找**：调 `lookupMapping` 看是否已有映射。
2. **更新引用计数或建新条目**：已存在则 `incRefCount`；不存在则分配设备内存、`emplace` 新条目。
3. **按需 H2D 搬运**：仅当有 `to` 标志、且（新条目 / `always` / 本区域新分配）时才把主机数据搬到设备。
4. **返回 TPR**：携带设备指针、各种标志。

它解决的核心问题之一是「何时复制」——答案就在第 3 步的条件里。

#### 4.4.2 核心流程

```
getTargetPointer(HstPtrBegin, Size, HasFlagTo, HasFlagAlways, ...):
  LR = lookupMapping(...)
  if IsContained (或隐式延伸):
      incRefCount()                      # 复用，计数 +1
      TargetPointer = TgtPtrBegin + (HstPtrBegin - Entry.HstPtrBegin)
  elif 显式延伸:        报错（explicit extension not allowed）
  elif 统一共享内存且非 close: 直接用主机指针（IsHostPointer=true）
  elif present 但不存在: 报错
  elif Size > 0:        # 真正的新映射
      TgtAllocBegin = Device.allocData(TgtPadding + Size)   # 设备分配
      TgtPtrBegin   = TgtAllocBegin + TgtPadding
      emplace 新 HostDataToTargetTy(...)                    # 登记进表
      IsNewEntry = true
  else:                 # Size==0 且不存在
      IsPresent = false

  # 「何时复制」的关键判断：
  if HasFlagTo && (IsNewEntry || HasFlagAlways || WasNewlyAllocatedForCurrentRegion) && Size:
      Device.submitData(TargetPointer, HstPtrBegin, Size, AsyncInfo)   # H2D
      addEventIfNecessary()                                            # 记录事件保序
```

注意几个细节：分配时多分配 `TgtPadding` 字节再偏移出 `TgtPtrBegin`，是为了对齐需求；`notifyDataMapped` 会通知插件层（例如某些后端的统一内存管理）。

#### 4.4.3 源码精读

**函数签名与整体结构**：

[libomptarget/OpenMP/Mapping.cpp:207-326](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L207-L326) —— `getTargetPointer` 的查找/建映射主体。重点看这几段：

- 已存在且复用、`incRefCount`：[L227-252](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L227-L252)。设备指针按偏移计算：`Ptr = Entry.TgtPtrBegin + (HstPtrBegin - Entry.HstPtrBegin)`。
- 新条目创建（含 `allocData` + `emplace` + `notifyDataMapped`）：[L293-321](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L293-L321)。

```cpp
uintptr_t TgtAllocBegin = (uintptr_t)Device.allocData(TgtPadding + Size, HstPtrBegin);
uintptr_t TgtPtrBegin = TgtAllocBegin + TgtPadding;
LR.TPR.setEntry(HDTTMap->emplace(new HostDataToTargetTy(
    (uintptr_t)HstPtrBase, (uintptr_t)HstPtrBegin,
    (uintptr_t)HstPtrBegin + Size, TgtAllocBegin, TgtPtrBegin,
    HasHoldModifier, HstPtrName)).first->HDTT);
```

**「何时复制」的判断与搬运**：

[libomptarget/OpenMP/Mapping.cpp:358-397](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L358-L397) —— 这就是实践任务关心的核心。条件 `HasFlagTo && (IsNewEntry || HasFlagAlways || WasNewlyAllocatedForCurrentRegion()) && Size != 0` 决定是否 `submitData`。`HasFlagAlways` 对应 `map(always, to:)`，即便条目已存在也会强制重传。该段还处理「多次新映射同一指针时避免覆盖影子指针」的边界（[L370-382](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L370-L382)）。

**搬运后的事件记录**：

[libomptarget/device.cpp:43-68](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L43-L68) —— `addEventIfNecessary`：若启用 `UseEventsForAtomicTransfers`，在首次 H2D 后创建并 `recordEvent`，存进条目的 `States->Event`。这个事件供后续相同条目的查询「等待」，保证 map 子句的原子性（4.6 详述）。

**上层调用语境**：

[libomptarget/omptarget.cpp:664-666](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L664-L666) —— `targetDataBegin` 主循环里对每个 `map` 条目调 `getTargetPointer`，传入从 `ArgTypes[I]` 解析出的 `HasFlagTo`/`HasFlagAlways` 等。注释提示「HDTTMap 将在 `getTargetPointer` 内部释放」。

#### 4.4.4 代码实践

**实践目标**：理解 `always` 如何在「已存在」时仍触发搬运。

**操作步骤**：

1. 阅读 [Mapping.cpp:358-361](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L358-L361) 的条件。
2. 对照下面的场景（**示例代码**，非项目原有）：

```c
int A[10] = {1,2,3,4,5,6,7,8,9,10};
#pragma omp target data map(to: A)            // (1) 新条目，DynRef 1，H2D 触发
{
  A[0] = 99;                                  // 主机改了 A[0]
  #pragma omp target data map(always,to: A)   // (2) 已存在，incRef 1->2，
  {                                           //     因 always，再次 H2D（把 99 传上去）
  }
}
```

**需要观察的现象 / 预期结果**：
- (1) 处：`IsNewEntry=true` → 满足复制条件 → 一次 H2D。
- (2) 处：`IsContained=true` 走 `incRefCount`（DynRef 1→2）；但因 `HasFlagAlways=true`，仍满足复制条件 → 再一次 H2D。若去掉 `always`，(2) 处不会搬运（既非新条目也非本区域新分配）。

**说明**：精确的搬运次数请用 `LIBOMPTARGET_INFO=4`（`OMP_INFOTYPE_DATA_TRANSFER`）实际运行确认；若无 GPU 可在 host 插件上观察（host 插件搬运多为 no-op，但 INFO 日志仍会打印）。

#### 4.4.5 小练习与答案

**练习 1**：`map(to:)` 一个**已经存在且引用计数 >0** 的变量（非 always），会发生 H2D 搬运吗？

**答案**：不会。条件里 `IsNewEntry=false`、`HasFlagAlways=false`、`WasNewlyAllocatedForCurrentRegion=false`，三选一全不满足，跳过 `submitData`。这正是 OpenMP 的优化：数据已在设备上且未被释放，无需重传，只做 `incRefCount`。

**练习 2**：`getTargetPointer` 在什么情况下返回的 `TargetPointer` 等于主机指针本身？

**答案**：当启用了统一共享内存（`OMP_REQ_UNIFIED_SHARED_MEMORY`）且没有 `close` 修饰符，或启用了 `OMPX_REQ_AUTO_ZERO_COPY` 时（见 [Mapping.cpp:265-285](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L265-L285)）。此时 `IsHostPointer=true`，直接返回 `HstPtrBegin`，不在设备上分配。

### 4.5 getTgtPtrBegin：引用计数递减与延迟删除（exit 路径）

#### 4.5.1 概念说明

exit 路径（`target data` / `target exit data` / `target` 区域退出）用的是 `getTgtPtrBegin` 的另一个重载（不带 `HDTTMap&`、自己拿锁的那个）。它做的事和 enter 路径镜像：

1. 查找映射。
2. 若命中：用 `decShouldRemove` 预测「这次递减后是否归零」（`IsLast`），然后 `decRefCount`。
3. 若 `ForceDelete`（`delete` 子句）：先 `resetRefCount` 再判断。
4. 若统一共享内存或未命中：按 OpenMP 5.2 的 firstprivate 语义返回主机指针。
5. **它只标记 `IsLast` 和递减计数，真正删除放在 `targetDataEnd` 后续步骤里**（延迟删除）。

「何时释放」的答案就在 `IsLast`：只有当条目的总引用计数即将归零、且没有其他线程还在引用它时，才允许删除。

#### 4.5.2 核心流程

```
getTgtPtrBegin(HstPtrBegin, Size, UpdateRefCount, UseHoldRefCount, MustContain, ForceDelete, FromDataEnd):
  HDTTMap = getExclusiveAccessor()            # 自己锁表
  LR = lookupMapping(...)
  if IsContained (或非 MustContain 时的延伸):
      IsLast = Entry.decShouldRemove(UseHoldRefCount, ForceDelete)   # 预测
      if ForceDelete: Entry.resetRefCount(UseHoldRefCount)           # delete: 先重置
      if FromDataEnd: Entry.incDataEndThreadCount()                  # 登记退出线程
      if UpdateRefCount:
          Entry.decRefCount(UseHoldRefCount)                         # 真正递减
          assert(IsLast 时总数已为 0)
      TargetPointer = TgtPtrBegin + (HstPtrBegin - Entry.HstPtrBegin)
  elif 统一共享内存: 返回主机指针
  else: IsPresent=false; 返回 HstPtrBegin（firstprivate 语义）
```

`IsLast` 这个标志随后被 `targetDataEnd` 用来决定「是否真的回收」：见下面的源码引用。

#### 4.5.3 源码精读

**函数实现**：

[libomptarget/OpenMP/Mapping.cpp:420-494](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L420-L494) —— `getTgtPtrBegin` 的递减/预测主体。关键段：

- `IsLast` 预测与 `ForceDelete` 重置：[L429-439](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L429-L439)。
- 退出线程计数（多线程 `targetDataEnd` 的延迟删除协调）：[L441-448](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L441-L448)。
- 真正的 `decRefCount` 与断言：[L449-463](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L449-L463)。

**删除条目（从表中移除）**：

[libomptarget/OpenMP/Mapping.cpp:510-530](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L510-L530) —— `eraseMapEntry`：带断言「总数必须为 0 且无其他退出线程引用」，然后 `HDTTMap->erase`。

**释放设备内存并销毁条目**：

[libomptarget/OpenMP/Mapping.cpp:532-556](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L532-L556) —— `deallocTgtPtrAndEntry`：销毁事件、`Device.deleteData(TgtAllocBegin)`、`notifyDataUnmapped`、`delete Entry`。

**上层：真正触发回收的代码**：

[libomptarget/omptarget.cpp:1135-1137](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1135-L1137) —— `targetDataEnd` 调 `getTgtPtrBegin(..., FromDataEnd=true)` 拿到 TPR（含 `IsLast`）。

[libomptarget/omptarget.cpp:1021-1027](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1021-L1027) —— 延迟删除的最终裁决：`IsNotLastUser = Entry->decDataEndThreadCount() != 0`，若 `DelEntry && (总数!=0 || IsNotLastUser)` 则**放弃删除**（让最后一个线程去删）。这解决了多线程同时退出同一区域的竞态。

[libomptarget/omptarget.cpp:1063-1066](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1063-L1066) —— 真正的回收：`eraseMapEntry` 后 `HDTTMap.destroy()` 放表锁，再 `deallocTgtPtrAndEntry`。

#### 4.5.4 代码实践

**实践目标**：追踪一次「内层退出不删、外层退出才删」的引用计数变化。

**操作步骤**（**示例代码**）：

```c
int A[100];
#pragma omp target data map(tofrom: A)   // (1) 新条目 DynRef=1
{
  #pragma omp target data map(tofrom: A) // (2) incRef -> DynRef=2
  {
    #pragma omp target map(tofrom: A)    // (3) incRef -> DynRef=3
    { /* kernel 改 A */ }
    // (3) 退出: decRef -> DynRef=2, IsLast=false, 不删
  }
  // (2) 退出: decRef -> DynRef=1, IsLast=false, 不删
}
// (1) 退出: decRef -> DynRef=0, IsLast=true, FROM 触发 D2H, 然后 erase+dealloc
```

1. 对照 [Mapping.cpp:429-463](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L429-L463) 手算每步的 `DynRefCount` 与 `IsLast`。
2. 注意 (3)(2) 退出时 `decShouldRemove` 因 `ThisRefCount` 仍 >1（递减前）返回 false；只有 (1) 退出时递减前 `DynRef=1`，`OtherRefCount=0`，返回 true。

**预期结果**：仅 (1) 退出会进入 [omptarget.cpp:1063-1066](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1063-L1066) 的回收分支。FROM 的 D2H 搬运在 post-processing 阶段完成（与条目删除解耦）。**待本地验证**：用 `LIBOMPTARGET_INFO` 观察实际的 `DynRefCount` 日志行（形如 `DynRefCount=2 (decremented)`）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `decShouldRemove` 在递减**之前**调用（而不是递减之后再判断 ==0）？

**答案**：因为调用方需要提前知道「这次递减会不会归零」，以便决定是否进入删除流程（设 `IsLast`、登记线程计数等）。`decShouldRemove` 把「当前值减 1 是否为 0」表达成 `ThisRefCount == 1`，从而在不动计数器的前提下预测结果。见 [Mapping.cpp:431-432](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L431-L432)。

**练习 2**：`delete` 子句对应的 `ForceDelete=true` 流程里，`resetRefCount` 之后为什么还要走一遍 `decShouldRemove(..., ForceDelete)` 做断言？

**答案**：`resetRefCount` 把计数器重置为 1（非 INF 时），那么「再减一次必归零」应当恒成立。断言 `IsLast == decShouldRemove(false)`（即 [Mapping.cpp:436-438](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/OpenMP/Mapping.cpp#L436-L438)）用来在调试期验证「重置后必然 IsLast」这一不变量，防止逻辑漂移。

### 4.6 影子指针、事件与 MappingConfig

#### 4.6.1 概念说明

映射机制还有三个辅助机制值得单独点出：

- **影子指针 `ShadowPtrInfoTy`**：当主机端有一个指针 `p` 指向被 map 的数据 `x`，进入设备区域时运行时要把设备上的 `p` 也改成指向设备上的 `x`（而非主机的 `x`）。退出时又要恢复主机 `p` 的原值。`ShadowPtrInfoTy` 记录「主机指针地址、设备指针地址、以及二者的原始内容」，用于这种「进入时改写、退出时还原」。它还能处理 Fortran 描述符（比 `void*` 大）。
- **事件 `Event`**：保证 map 子句原子性的同步原语。首次 H2D 后记录事件，后续对同一条目的查询若发现事件存在就 `waitEvent`，避免读到旧数据。
- **`MappingConfig`**：进程级单例，集中管理映射相关的环境变量开关，如 `LIBOMPTARGET_MAP_FORCE_ATOMIC`（是否用事件保原子性）和 `LIBOMPTARGET_TREAT_ATTACH_AUTO_AS_ALWAYS`。

#### 4.6.2 核心流程

```
进入时若 HasFlagTo 且复制发生:
  addEventIfNecessary() -> createEvent + recordEvent, 存入 States->Event
后续查询同一条目(非新、非always):
  if Event 存在: Device.waitEvent(Event, AsyncInfo)   # 等待之前的 H2D 完成
退出时若有 FROM:
  foreachShadowPointerInfo: 用 HstPtrContent 还原主机指针原值
```

#### 4.6.3 源码精读

**影子指针结构**：

[include/OpenMP/Mapping.h:60-107](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L60-L107) —— `ShadowPtrInfoTy`。构造时把主机指针当前指向的「被指对象基址」拷进 `HstPtrContent`/`TgtPtrContent` 的前 `VoidPtrSize` 字节；若 `PtrSize > VoidPtrSize`（Fortran 描述符），额外拷贝剩余字段。`HstPtrAddr` 作为唯一键（`operator==`/`operator<`）。

**事件字段与影子指针集合**：

[include/OpenMP/Mapping.h:150-166](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L150-L166) —— `StatesTy` 里的 `ShadowPtrInfos`（`SmallSet<ShadowPtrInfoTy, 2>`）、`Event`、`DataEndThreadCount`。注释说明事件目前仅用于 H2D 方向。

**影子指针的注册与遍历**：

[include/OpenMP/Mapping.h:290-325](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L290-L325) —— `addShadowPointer`（含 Fortran 描述符的陈旧条目替换逻辑）与 `foreachShadowPointerInfo`。退出时的还原逻辑见 [omptarget.cpp:1034-1054](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1034-L1054)（`memcpy(ShadowPtr.HstPtrAddr, HstPtrContent, PtrSize)`）。

**配置单例**：

[include/OpenMP/Mapping.h:31-58](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L31-L58) —— `MappingConfig`。构造函数读取两个 `BoolEnvar`，分别决定 `UseEventsForAtomicTransfers`（默认 true）与 `TreatAttachAutoAsAlways`（默认 false）。`get()` 返回静态单例。

#### 4.6.4 代码实践

**实践目标**：理解事件开关如何被环境变量控制。

**操作步骤**：

1. 阅读 [Mapping.h:33-40](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/include/OpenMP/Mapping.h#L33-L40) 与 [device.cpp:45-47](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/device.cpp#L45-L47)：`addEventIfNecessary` 开头检查 `UseEventsForAtomicTransfers`，若为 false 直接返回成功（不创建事件）。
2. 思考：设置 `LIBOMPTARGET_MAP_FORCE_ATOMIC=0` 会怎样？

**预期结果**：运行时不再为 map 子句创建/等待事件，map 操作不再保证跨线程的原子顺序（在某些有数据竞争的场景下可能更快但不安全）。默认 true 是安全选项。

#### 4.6.5 小练习与答案

**练习**：影子指针为什么要在退出时「还原主机指针原值」？

**答案**：进入区域时运行时把设备上的指针改写成了设备地址，但主机端的指针变量在区域结束后仍可能被主机代码使用，必须还原成它最初指向的主机地址，否则主机代码会拿到一个对主机无意义的设备地址。`HstPtrContent` 保存的就是这个「原始主机值」。见 [omptarget.cpp:1029-1053](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1029-L1053) 的注释。

## 5. 综合实践

把本讲的知识串起来：用一段嵌套的 `target data` 程序，**完整追踪一个数组 `A` 的 `HostDataToTargetTy` 条目从创建到销毁的全过程**，重点记录每一步的引用计数与「是否搬运 / 是否删除」判断。

**示例代码**（非项目原有，用于追踪）：

```c
#include <stdio.h>
int main() {
  int A[100];
  for (int i = 0; i < 100; ++i) A[i] = i;

  // (1) enter: map(to: A)        新条目
  #pragma omp target data map(to: A[0:100])
  {
    // (2) enter: map(always, to: A[0:50])  已存在 + always
    #pragma omp target data map(always,to: A[0:50])
    {
      // (3) enter+exit: map(tofrom: A[0:10])  已存在
      #pragma omp target map(tofrom: A[0:10])
      { /* 设备内核：对 A[0:10] 加倍 */ }
      // (3) exit: decRef，非 IsLast
    }
    // (2) exit: decRef，非 IsLast
  }
  // (1) exit: map(from: 隐含于 tofrom? 注意 map(to) 退出不回传)
  return 0;
}
```

**你要完成的事**：

1. **画状态表**：为 (1)(2)(3) 各自的 enter 与 exit，填写下表（以 `DynRefCount` 为主，`HoldRefCount` 全程为 0）：

| 步骤 | 操作前 DynRef | 触发的函数 | 关键判断 | 是否 H2D/D2H | 操作后 DynRef | IsLast |
|------|--------------|-----------|---------|-------------|--------------|--------|
| (1) enter | （无条目） | `getTargetPointer` | `IsNewEntry=true` + `HasFlagTo` | H2D ×1 | 1 | — |
| (2) enter | 1 | `getTargetPointer` | `IsContained` + `HasFlagAlways` | H2D ×1 | 2 | — |
| (3) enter | 2 | `getTargetPointer` | `IsContained`，非 always | 无 | 3 | — |
| (3) exit | 3 | `getTgtPtrBegin` | `decShouldRemove`=`ThisRef==1`?否 | 无 | 2 | false |
| (2) exit | 2 | `getTgtPtrBegin` | 同上，否 | 无 | 1 | false |
| (1) exit | 1 | `getTgtPtrBegin` | `ThisRef==1 && Other==0` → 是 | 取决于 FROM 标志 | 0 | true → erase+dealloc |

2. **验证关键不变量**：
   - 只有 `IsLast=true` 的那次 exit 才会走到 [omptarget.cpp:1063-1066](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1063-L1066) 的 `eraseMapEntry`+`deallocTgtPtrAndEntry`。
   - `map(to:)` 在 (1) 的 exit 处**没有 FROM 标志**，所以即便 `IsLast` 也不回传数据（注意：`tofrom` 才会 FROM；本例 (1) 是纯 `to`，退出时只删条目、不搬运）。请对照 [omptarget.cpp:1034](https://github.com/llvm/llvm-project/blob/0e87f69d0c327612590f73b715c657c5096383c2/offload/libomptarget/omptarget.cpp#L1034) 的 `HasFrom` 判断确认。

3. **运行验证**（若本机有可用的 OpenMP 卸载工具链，host 插件亦可）：

```bash
clang -fopenmp -fopenmp-targets=<本机三元组> demo.c -o demo
LIBOMPTARGET_INFO=63 ./demo 2>&1 | grep -E "Mapping|Copying|RefCount"
```

观察日志中的 `Creating new map entry`、`Mapping exists ... DynRefCount=N (incremented/decremented)`、`Copying data from host to device` 等行，与你手画的表对照。**若无工具链或结果不确定，明确标注「待本地验证」**，仅以源码逻辑为准。

4. **进阶**：把 (1) 改成 `map(tofrom: A)`，重新分析 (1) exit 处是否会触发 D2H（提示：`HasFrom=true` 且 `IsLast=true`）。这能帮你厘清「释放（删条目）」与「回传（FROM 搬运）」是两件独立的事。

## 6. 本讲小结

- 映射表 `HDTTMap` 是一张按 `HstPtrBegin` 排序的 `std::set`，每条 `HostDataToTargetTy` 记录一段「主机区间 ↔ 设备区间」绑定，外加 **动态 + 保持双引用计数** 状态机；INF 计数表示「永不释放」（如 `omp_target_associate_ptr`）。
- 「何时复制」由 enter 路径 `getTargetPointer` 决定：仅当 `HasFlagTo && (IsNewEntry || HasFlagAlways || WasNewlyAllocatedForCurrentRegion)` 才 H2D 搬运；`always` 是在「已存在」时仍强制重传的唯一开关。
- 「何时释放」由 exit 路径 `getTgtPtrBegin` 决定：`decShouldRemove` 预测「这次递减是否归零」，`IsLast=true` 才进入延迟删除；多线程退出时靠 `DataEndThreadCount` 确保只有最后一个线程真正 `eraseMapEntry`+`deallocTgtPtrAndEntry`。
- 并发安全靠「表锁（`ProtectedObj`/`Accessor`）+ 条目锁（`HostDataToTargetTy::Mtx`）」两层，锁序固定为「先表后条目」；`TargetPointerResultTy` 用 RAII 在持有期间锁住条目，从而允许「放表锁、持条目锁」搬运数据，缩小临界区。
- `lookupMapping` 用 `upper_bound` + 前驱判断实现区间查找，区分 `IsContained/ExtendsBefore/ExtendsAfter`；`Size==0` 时还支持按 `HstPtrBase` 命中。
- 影子指针 `ShadowPtrInfoTy` 处理「指针指向被 map 数据」的进入改写/退出还原（含 Fortran 描述符）；事件 `Event` 配合 `MappingConfig::UseEventsForAtomicTransfers` 保证 map 子句的 H2D 原子性。

## 7. 下一步学习建议

本讲把「单条 map 在映射层的处理」讲透了，但还没讲「一个 `target data` 指令的所有 map 条目如何**协同**处理」。建议下一步学习 [u2-l5 target data begin/end/update 流程](u2-l5-target-data-flow.md)，它会把本讲的 `getTargetPointer`/`getTgtPtrBegin` 放回 `targetDataBegin`/`targetDataEnd`/`targetDataUpdate` 的主循环里，讲清：

- `map` 子句如何被解析成 `ArgTypes` 数组、成员隶属关系（`OMP_TGT_MAPTYPE_MEMBER_OF`）如何影响 `UpdateRef`；
- `StateInfoTy`（本讲已见其声明）如何在一个 construct 内跨递归 mapper 调用追踪新分配、跳过的 FROM、已回传的 FROM，避免重复搬运；
- begin/end/update 三个阶段在搬运时机上的差异。

读完 u2-l5，你就能把「单条映射」与「整条指令」两层拼成完整的 OpenMP 数据移动图景。之后 [u2-l6 内核启动流程](u2-l6-kernel-launch-flow.md) 会衔接「数据就位后如何启动 kernel」，完成 enter→kernel→exit 的闭环。
