# 二进制发现与特殊 section 解析

## 1. 本讲目标

本讲是单元 3「主处理链路」的第二篇。上一篇（u3-l1）我们俯瞰了 `RewriteInstance::run()` 的 20 个阶段；本讲钻进这条流水线的最开头三步，回答一个具体问题：**BOLT 是怎么「认出」输入 ELF 里有哪些函数、哪些 section 的？**

学完后你应当能够：

- 说清 `discoverStorage()`、`readSpecialSections()`、`discoverFileObjects()` 三个阶段各自干什么、为什么是这个先后顺序。
- 解释 `BinarySection` 作为「BOLT 对一个 section 的统一抽象」持有哪些状态，以及它如何被 `BinaryContext` 索引（按地址、按名字、按 `SectionRef`）。
- 描述一个 ELF 符号是如何一步步变成一个 `BinaryFunction` 对象的，特别是 **FILE 符号如何用来给同名局部函数消歧**。
- 列出 `readSpecialSections()` 至少识别的 3 类特殊 section（`.eh_frame`、`.rela.text`、`.symtab`、BAT、debug 等）及其用途。

本讲只覆盖「发现」阶段——它只负责建索引、认对象，**还不做反汇编、不建 CFG**。反汇编与 CFG 重建是下一篇 u3-l3 的主题。

## 2. 前置知识

在进入源码前，先建立几个直观念懂的概念。

### 2.1 ELF 文件里有什么

一个 ELF 可执行文件大致由三部分构成：

- **程序头表（Program Header Table，PT_LOAD 等）**：站在「加载器」视角，描述文件里哪一段字节要被映射到进程虚拟地址空间的哪个地址（`p_vaddr`）、多大（`p_memsz`/`p_filesz`）、什么权限（可读/可写/可执行）。
- **节区头表（Section Header Table）**：站在「链接器/工具」视角，把文件切成一个个有名字的 section，比如 `.text`（代码）、`.data`（已初始化数据）、`.rodata`（只读数据）、`.eh_frame`（异常处理表）、`.symtab`（符号表）、`.rela.text`（针对 `.text` 的重定位）。
- **符号表（.symtab）**：一张「名字 ↔ 地址」的对照表。每个符号有一个类型，本讲最关心三种：`STT_FUNC`（函数）、`STT_OBJECT`（数据对象）、`STT_FILE`（文件，标记「下面的符号来自哪个源文件」）。

BOLT 的「发现」阶段，本质就是把这三张表读进自己的内存数据结构里。

### 2.2 为什么 BOLT 要自己重新建一套索引

LLVM 自带的 `object::ObjectFile`/`SectionRef`/`SymbolRef` 已经能读 ELF，为什么 BOLT 不直接用，而要再造 `BinarySection`、`BinaryData`、`BinaryFunction`？

因为 BOLT 要**改写**二进制：它会移动函数、重排 section、新增 section、删调试信息。原生的 `SectionRef` 是只读视图，描述的是「输入文件里现在长什么样」；BOLT 需要一个既能记录输入状态、又能承载输出状态（`OutputAddress`/`OutputSize`/`OutputContents`）的可变抽象，还要能在移动后按新地址快速查找。所以 BOLT 在原生层之上包了一层自己的对象模型——这正是 u2-l1 提到的「四套函数索引」的由来。

### 2.3 三个阶段为什么是这个顺序

`run()` 的开头是这样三行（外加一个 `adjustCommandLineOptions`）：

```cpp
if (Error E = discoverStorage())      // 1. 先摸清地址空间与旧 .text
  return E;
if (Error E = readSpecialSections()) // 2. 再把所有 section 注册进来、识别特殊 section
  return E;
adjustCommandLineOptions();
discoverFileObjects();               // 3. 最后才扫符号表认函数
```

顺序的逻辑是「**先有地址空间底图 → 再有 section 索引 → 才能认函数**」。认函数时要回答「这个符号落在哪个 section 里」「这个地址是不是在 `.text` 内」，这些都依赖前两步已经建好的索引，所以函数发现必须放最后。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Rewrite/RewriteInstance.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp) | 本讲主战场。包含 `run()`、`discoverStorage()`、`readSpecialSections()`、`discoverFileObjects()` 四个函数。 |
| [include/bolt/Core/BinarySection.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinarySection.h) | `BinarySection` 类定义：section 的统一抽象与一堆判定谓词。 |
| [lib/Core/BinaryContext.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp) | `registerSection()`、`getSectionForAddress()`、`createBinaryFunction()`、`registerNameAtAddress()` 的实现——把 section/符号挂进全局索引的地方。 |
| [lib/Core/BinaryData.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryData.cpp) | `BinaryData`（「一个有名字的数据对象」）的实现，是符号在 BOLT 里的另一重身份。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先讲 `discoverStorage` 建地址底图，再讲 `BinarySection` 这个数据结构本身，接着讲 `readSpecialSections` 如何注册 section 并识别特殊 section，最后讲 `discoverFileObjects` 如何把符号变成函数。

### 4.1 discoverStorage：建立地址空间底图

#### 4.1.1 概念说明

`discoverStorage()` 是 `run()` 调用的第一个实质阶段。它的任务不是「认函数」，而是回答几个关于**整体存储布局**的全局问题：

1. 这个二进制的虚拟地址空间从哪开始、到哪结束？（`FirstAllocAddress`、`NextAvailableAddress`）
2. 有哪些 `PT_LOAD` 段？各自的地址、大小、权限是什么？（`SegmentMapInfo`）
3. 旧的 `.text`（主代码 section）在文件里的位置和地址是什么？（`OldTextSectionAddress/Size/Offset`）
4. **这个文件是不是已经被 BOLT 处理过？**（防止对 BOLT 产物二次优化）
5. 新代码应该从哪个地址开始写？（为新 PHDR 表、新 `.text` 段预留空间）

把这些算清楚，后续阶段才有「坐标系」可用。

#### 4.1.2 核心流程

```
discoverStorage()
 ├─ 遍历所有 PT_LOAD 段：
 │    ├─ 更新 FirstAllocAddress（取最小）
 │    ├─ 更新 NextAvailableAddress/Offset（取最大末端）
 │    ├─ 记录 SegmentMapInfo[vaddr] = {大小, 偏移, 权限...}
 │    └─ x86-64 且地址很高 → 判定为 Linux kernel
 ├─ 遍历所有 section：
 │    ├─ 找到主代码 section → 记录 OldTextSection*
 │    └─ 若发现 ".bolt.org" 前缀或 BOLT 的 .text → 报错「已被 BOLT 处理」
 ├─ 对齐 NextAvailableAddress/Offset 到页边界
 ├─ （可选）为新 PHDR 表预留空间
 └─ 校验 .text 确实被映射到文件 → 否则报错
```

关键不变量：`NextAvailableAddress` 表示「新内容可以从这里开始往高地址写」，它是后面 `emitAndLink` 阶段决定新代码落点的基础。

#### 4.1.3 源码精读

入口与计时器，注意每个阶段都用 `NamedRegionTimer`（RAII）自动计时，由 `-time-rewrite` 开关启用——这正是 u3-l1 提到的计时机制：

[lib/Rewrite/RewriteInstance.cpp:609-616](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L609-L616) —— `discoverStorage` 是个模板函数（按 ELF 位宽 32/64 实例化），一进来就记录入口地址 `e_entry` 并清零 `NextAvailableAddress`。

遍历 `PT_LOAD` 段，建立段信息表并算出地址空间末端：

[lib/Rewrite/RewriteInstance.cpp:625-653](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L625-L653) —— 对每个 `PT_LOAD`，用 `std::min/max` 更新「最低分配地址」与「最高末端地址」，并把段信息存进 `BC->SegmentMapInfo`；同时检测 `PT_INTERP`（动态链接解释器）。`NextAvailableAddress` 就是所有段末端的**最大值**。

防止对 BOLT 产物二次优化——这是最重要的安全校验之一：

[lib/Rewrite/RewriteInstance.cpp:675-682](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L675-L682) —— 如果某个 section 名以 `.bolt.org`（`getOrgSecPrefix()`）开头，或出现了 BOLT 自己的 `.text` 段名，说明这文件已被 BOLT 处理过，直接返回 `function_not_supported` 错误。原因：BOLT 输出的二进制布局已被打乱，再优化会出错。

页对齐并（可选）预留新 PHDR 表空间：

[lib/Rewrite/RewriteInstance.cpp:712-722](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L712-L722) —— 把 `NextAvailableAddress` 对齐到页边界；若开了 `-hugify` 且非固定加载地址，再多留一页（应对 ASLR 的 4KB 对齐怪相）。`NewTextSegmentAddress/Offset` 就此定下。

最后的健全性检查——确保 `.text` 真的被映射进文件（`objcopy` 等工具可能剥掉内容却留下头部）：

[lib/Rewrite/RewriteInstance.cpp:776-781](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L776-L781) —— 若 `.text` 地址查不到文件偏移，判定为非法 ELF 并报错。

#### 4.1.4 代码实践

**实践目标**：理解 `discoverStorage` 如何判定「二进制已被 BOLT 处理」以及它如何为新代码选定起始地址。

**操作步骤**（源码阅读型）：

1. 打开 [lib/Rewrite/RewriteInstance.cpp:675-682](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L675-L682)，找到 `getOrgSecPrefix()` 的调用。
2. 用 Grep 搜 `getOrgSecPrefix` 的定义，看它返回的字符串前缀是什么。
3. 跟踪 `NextAvailableAddress` 这个变量从 618 行到 781 行的所有赋值点，画出它的变化轨迹。

**需要观察的现象**：`NextAvailableAddress` 一开始为 0，在 `PT_LOAD` 循环里被推到「所有段末端的最大值」，再被页对齐，可能再为 PHDR 表/Hugify 加偏移。

**预期结果**：你能用一句话说出「新代码段的起始地址 = max(所有 PT_LOAD 末端) 经页对齐后的值」。若无法本地运行，明确标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `discoverStorage` 要把 `NextAvailableAddress` 设成所有 `PT_LOAD` 段末端的「最大值」而不是「最小值」？

> **答案**：新写的代码段必须放在所有已映射段之后，否则会覆盖原有内容。取最大末端才能保证新内容落在已用地址空间之外的空闲高地址区。

**练习 2**：`BC->IsLinuxKernel` 是在哪个分支里被置真的？依据是什么？

> **答案**：在 `PT_LOAD` 分支里（[645-647 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L645-L647)），当架构是 x86-64 且段地址 ≥ `KernelStartX86_64` 时判定为 Linux kernel。因为内核映射在极高的虚拟地址上，与普通用户态程序地址范围不同。

---

### 4.2 BinarySection：BOLT 对 ELF section 的统一抽象

#### 4.2.1 概念说明

`BinarySection` 是 BOLT 给「一个 section」套的壳。原生 `SectionRef` 只能告诉你输入文件里这个 section 长什么样；而 `BinarySection` 同时持有：

- **输入态**：名字、地址、大小、内容（`Contents`）、对齐、ELF 类型与 flags（`ELFType`/`ELFFlags`）。
- **重定位**：该 section 上的普通重定位、动态重定位、待处理重定位（`Relocations`/`DynamicRelocations`/`PendingRelocations`）。
- **输出态**：输出名字、输出地址、输出大小、输出内容、是否已 finalize（`OutputAddress`/`OutputSize`/`OutputContents`/`IsFinalized`）。
- **一组判定谓词**：`isText()`、`isData()`、`isAllocatable()`、`isWritable()`、`isVirtual()`、`isRelro()` 等，把 ELF flags 翻译成人话。

它是后续所有「移动代码/数据」操作的载体——BOLT 重排函数后，新的 `.text` 内容就是写进某个 `BinarySection` 的 `OutputContents` 里的。

#### 4.2.2 核心流程

一个 `BinarySection` 的生命周期：

```
输入 ELF 的 SectionRef
   │  BinaryContext::registerSection(SectionRef)
   ▼
new BinarySection(BC, Section)   // 拷出 Name/Address/Size/Contents/ELFType/ELFFlags
   │  注册进三套索引：
   │    AddressToSection   (按地址查)
   │    NameToSection      (按名字查)
   │    SectionRefToBinarySection (按原生 SectionRef 查)
   ▼
后续阶段读写它：加重定位、改内容、设定输出地址...
   │  emit/rewrite 阶段
   ▼
finalize：把 OutputContents 落盘到输出文件
```

#### 4.2.3 源码精读

类的核心成员——注意输入态（前半）与输出态（后半）的分野：

[include/bolt/Core/BinarySection.h:48-101](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinarySection.h#L48-L101) —— `Name/Address/Size/Contents/ELFType/ELFFlags` 描述输入；`Relocations/DynamicRelocations/Patches` 承载改动；`IsFinalized/OutputAddress/OutputSize/OutputContents/SectionID` 描述输出。这个「输入态 + 输出态合一」的设计，正是 BOLT 能原地改写 section 的关键。

构造函数如何从原生 `SectionRef` 抽取信息：

[include/bolt/Core/BinarySection.h:157-172](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinarySection.h#L157-L172) —— 从 `SectionRef` 取名字、地址、大小、对齐；若是 ELF，再额外取 `ELFType`（如 `SHT_PROGBITS`/`SHT_NOBITS`/`SHT_RELA`）和 `ELFFlags`（如 `SHF_ALLOC`/`SHF_WRITE`/`SHF_EXECINSTR`）以及文件偏移。注意 `Contents` 用 `getContentsOrQuit()` 一次性把 section 字节拷进内存（`SHT_NOBITS` 的 `.bss` 返回空）。

判定谓词把 ELF flags 翻译成语义——后续「认函数」时判断「符号是否在代码段」就靠 `isText()`/`isAllocatable()`：

[include/bolt/Core/BinarySection.h:259-287](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinarySection.h#L259-L287) —— `isText()` 看 `SHF_EXECINSTR`；`isAllocatable()` 看 `SHF_ALLOC` 且非 TBSS；`isVirtual()` 对应 `SHT_NOBITS`（`.bss`，文件里不占字节）；`isWritable()` 看 `SHF_WRITE`。

注册逻辑——一个 section 被同时塞进三套索引：

[lib/Core/BinaryContext.cpp:2365-2385](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp#L2365-L2385) —— `registerSection(SectionRef)` 先 `new` 一个 `BinarySection`，再委托给 `registerSection(BinarySection*)`：只有**可分配且有地址**的 section 才进 `AddressToSection`（按地址查）；所有 section 都进 `NameToSection`（按名字查，允许重名所以是 multimap）；带 `SectionRef` 的还进 `SectionRefToBinarySection`。`Sections` 这个 `set` 用指针去重，断言「同一个 section 不能注册两次」。

按地址反查 section——`discoverFileObjects` 判定符号归属时就调它：

[lib/Core/BinaryContext.cpp:2345-2356](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp#L2345-L2356) —— 经典的「`upper_bound` 后退一格」区间查找：在按起始地址排序的 `AddressToSection` 里找到第一个起始地址 > 目标地址的项，后退一格，再检查目标地址是否落在 `[起始, 起始+大小)` 内。零大小 section 特殊处理（上界 +1）。

#### 4.2.4 代码实践

**实践目标**：验证「可分配 section 才进 `AddressToSection`」这一设计。

**操作步骤**（源码阅读型）：

1. 读 [lib/Core/BinaryContext.cpp:2365-2381](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp#L2365-L2381)，注意 `isAllocatable() && getAddress()` 这个条件。
2. 思考：`.symtab`、`.debug_info`、`.comment` 这类**非可分配** section 会不会进 `AddressToSection`？
3. 再读 [lib/Core/BinaryContext.cpp:2345-2356](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp#L2345-L2356) 的 `getSectionForAddress`。

**需要观察的现象**：非可分配 section（如 `.debug_info`）通常没有运行时地址（`getAddress()` 为 0），即使有也不会进 `AddressToSection`。

**预期结果**：你能解释「为什么用地址查不到调试 section」——因为它们不在地址索引里，要按名字查（`getUniqueSectionByName`）。这是 BOLT 刻意的取舍：地址索引只服务于「会被加载进内存」的 section。

#### 4.2.5 小练习与答案

**练习 1**：`isVirtual()` 返回 true 的 section 有什么特点？为什么它的 `Contents` 是空的？

> **答案**：`isVirtual()` 对应 `SHT_NOBITS`，即 `.bss` 类 section。它在文件里不占字节（运行时由加载器清零分配），所以 `getContentsOrQuit()` 对它返回空 `StringRef`，`Contents` 自然为空。

**练习 2**：为什么 `NameToSection` 允许重名（用 multimap），而 `Sections` 用 set 去重？

> **答案**：同名 section 在 ELF 里是合法的（比如多个 `.text.foo`、链接器合并产生的重名），所以按名字查要能返回多个，用 multimap。但每个 `BinarySection` 对象是独一无二的实体，`Sections` 这个 set 用对象指针去重，保证「同一个对象不会被注册两次」。

---

### 4.3 readSpecialSections：section 注册与特殊 section 解析

#### 4.3.1 概念说明

`readSpecialSections()` 紧跟在 `discoverStorage()` 之后，做两件事：

1. **把输入文件的所有 section 注册成 `BinarySection`**（调 `BC->registerSection`），建好 u2-l1 提到的 section 索引。
2. **识别并处理若干「特殊」section**，它们携带 BOLT 后续阶段必需的元数据，例如：
   - `.eh_frame` —— 异常处理/栈展开表（CIE/FDE），里面记录了函数的地址范围，BOLT 用它辅助确定函数边界。
   - `.rela.text` / `.crel.text` —— 针对 `.text` 的重定位。**有没有它，决定了 BOLT 能不能进入「重定位模式」**（即能否重排函数），这正是 u1-l3 讲的 `--emit-relocs` 的落点。
   - `.symtab` —— 符号表。**没有它，二进制就是 stripped 的**，BOLT 默认拒绝处理。
   - `.note.bolt_bat` —— BAT（Bolt Address Translation）section，标志这个文件已被 BOLT 优化过并带地址翻译表（u4-l3 详述）。
   - 各类 `.debug_*` —— 调试信息；若开启 `-update-debug-sections` 则必须未压缩。

它还顺带读 `.eh_frame` 构造 `CFIReaderWriter`，为后面 `discoverFileObjects` 用 FDE 校验函数边界做准备。

#### 4.3.2 核心流程

```
readSpecialSections()
 ├─ 遍历所有 section：
 │    ├─ 若是 debug section：标记 HasDebugInfo；若要更新且被压缩 → 报错
 │    └─ BC->registerSection(Section)        // 全部注册
 ├─ markGnuRelroSections()                   // 按 PT_GNU_RELRO 标记 relro
 ├─ 检测 .ltext → 启用大代码模型
 ├─ 探测特殊 section（用 getUniqueSectionByName）：
 │    ├─ .rela.text / .crel.text → HasTextRelocations
 │    ├─ .symtab                → HasSymbolTable
 │    ├─ .eh_frame              → EHFrameSection
 │    └─ .note.bolt_bat         → 解析 BAT 表
 ├─ 综合：BC->HasRelocations = HasTextRelocations && 用户未禁用
 ├─ BC->IsStripped = !HasSymbolTable          // 无符号表即被 strip
 ├─ 读 .eh_frame → 构造 CFIReaderWriter       // 供 discoverFileObjects 校验函数边界
 ├─ processSectionMetadata()                  // 处理 section 级元数据脚本
 └─ readELFDynamic()                          // 读 .dynamic / PT_DYNAMIC
```

#### 4.3.3 源码精读

主循环——逐个注册 section，同时挑出调试 section 做压缩校验：

[lib/Rewrite/RewriteInstance.cpp:2391-2417](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2391-L2417) —— 对每个 section 先 `getContents()`（顺便把错误冒泡出去），再 `BC->registerSection(Section)` 把它变成 `BinarySection`。若开了 `-update-debug-sections` 却遇到压缩的调试 section，直接返回 `not_supported` 错误——因为 BOLT 的 DWARF 重写器（u8-l1）只处理未压缩的调试信息。

注意第 2411 行的 `BC->registerSection(Section)`，它就是 4.2 节那个把 section 塞进三套索引的入口：

[lib/Rewrite/RewriteInstance.cpp:2411](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2411) —— 一行调用，背后是 `new BinarySection` + 三套索引注册。

探测四类关键特殊 section——全用 `getUniqueSectionByName` 按名字查（因为它们大多不可分配，不在地址索引里）：

[lib/Rewrite/RewriteInstance.cpp:2434-2453](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2434-L2453) —— `.rela`/`.crel` 拼上主代码 section 名（`.text`）得到 `.rela.text`，存在即 `HasTextRelocations`；`.symtab` 存在即 `HasSymbolTable`；`.eh_frame` 存进 `EHFrameSection`；若发现 `.note.bolt_bat` 就解析 BAT 表（heatmap 独占模式除外）。

把探测结果综合成两个全局开关——这是整段的「决策点」：

[lib/Rewrite/RewriteInstance.cpp:2468-2483](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2468-L2483) —— `BC->HasRelocations = HasTextRelocations && 用户未禁用`（AArch64 非 reloc 模式会警告，Linux kernel 强制关闭 reloc 模式）；`BC->IsStripped = !HasSymbolTable`，若被 strip 且未显式 `--allow-stripped` 则直接 `exit(1)`。**这两行决定了 BOLT 接下来能做什么**：有 reloc 才能重排函数，没 strip 才能认符号。

读 `.eh_frame` 构造 CFI 读写器——为下一步 `discoverFileObjects` 用 FDE 校验函数边界铺路：

[lib/Rewrite/RewriteInstance.cpp:2493-2497](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2493-L2497) —— 从 `DwCtx` 取出 `EHFrame`（DWARF 格式的 `.eh_frame`），交给 `CFIReaderWriter`。后者把 CIE/FDE 解析成可查询的函数地址范围表，`discoverFileObjects` 会用它来发现「符号表里没标、但 FDE 里有」的函数边界冲突。

#### 4.3.4 代码实践

**实践目标**：亲手确认一个真实二进制里有哪些特殊 section，并对照源码理解每个会被 `readSpecialSections` 如何处理。

**操作步骤**（命令行 + 源码阅读）：

1. 找一个带 `--emit-relocs` 链接的小程序（或参照 u1-l3 的 hello world）。
2. 运行 `llvm-readelf -S <binary> | grep -E "eh_frame|rela.text|symtab|bolt_bat|debug_info"`，列出命中的 section。
3. 对照 [lib/Rewrite/RewriteInstance.cpp:2434-2453](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2434-L2453)，逐个标注每个命中的 section 会被赋给哪个变量（`HasTextRelocations`/`HasSymbolTable`/`EHFrameSection`/`HasBATSection`/`HasDebugInfo`）。

**需要观察的现象**：

- 用 `-Wl,-q` 链接的二进制应当能看到 `.rela.text`；不带该选项的则没有。
- strip 过的二进制（`strip` 命令处理后）没有 `.symtab`。

**预期结果**：你能填出下面这张表（示例答案）：

| section | 变量 | 含义 |
| --- | --- | --- |
| `.rela.text` | `HasTextRelocations` | 决定能否进入重定位模式（重排函数） |
| `.symtab` | `HasSymbolTable` | 决定 `IsStripped`，无它默认拒绝处理 |
| `.eh_frame` | `EHFrameSection` | 异常处理表，辅助确定函数边界 |
| `.note.bolt_bat` | `HasBATSection` | 标志文件已被 BOLT 优化过，含地址翻译表 |

若本地没有可链接环境，标注「待本地验证」并改为纯源码阅读：在 [2434-2453 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2434-L2453) 列出全部 `getUniqueSectionByName` 调用及其目标变量。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `.rela.text` 的探测要用「`.rela` + 主代码 section 名」拼接，而不是直接写死 `.rela.text`？

> **答案**：主代码 section 名通常是 `.text`，但在某些场景（如 Linux kernel、自定义链接脚本）可能不同。`getMainCodeSectionName()` 让这个拼接与实际主代码段名保持一致，更稳健。

**练习 2**：若输入二进制既没有 `.symtab` 也没有 `.rela.text`，BOLT 默认会怎样？

> **答案**：没有 `.symtab` → `IsStripped = true`，默认 `exit(1)` 拒绝处理（除非 `--allow-stripped`）；没有 `.rela.text` → `HasRelocations = false`，BOLT 退化为「非重定位模式」，只能做有限优化、不能重排函数顺序（若用户显式 `-relocs` 还会因 [2460-2466 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L2460-L2466) 的检查而 `exit(1)`）。

**练习 3**：`.eh_frame` 里的 FDE 在本阶段派什么用场？

> **答案**：被 `CFIReaderWriter` 解析成一张「函数起始地址 → 地址范围」的表（FDE 记录每个函数的 `initialLocation` 和 `addressRange`）。下一步 `discoverFileObjects` 会用它校验符号表里的函数边界，发现「符号地址落在某个 FDE 范围中间」这类冲突，从而把函数标记为非 simple。

---

### 4.4 discoverFileObjects：从符号表发现函数

#### 4.4.1 概念说明

`discoverFileObjects()` 是本讲的重头戏。它遍历输入文件的符号表，把每一个**函数符号**变成一个 `BinaryFunction` 对象，把每一个**数据符号**变成一个 `BinaryData`（经 `registerNameAtAddress`）。这是 u2-l1 那个「`BinaryFunctions` 映射」最初的填充点。

这里有个真实世界的麻烦：**C/C++ 允许不同源文件里有同名的 `static` 函数**（它们是局部符号，互不相干）。ELF 符号表里它们名字一样、作用域是 local，光看名字分不清谁是谁。BOLT 的解法是用 **`STT_File` 符号**——ELF 规定 file 符号标记「接下来的符号来自某个源文件」，于是 BOLT 可以给同名局部函数拼出一个「函数名/文件名/编号」的唯一名字，既能区分，又能和 perf profile（fdata 格式正是 `函数/文件/行`）对上。

它还要处理一堆边角：汇编函数没有类型标记、大小为 0；PLT stub 符号要跳过（由专门的 `disassemblePLT` 处理）；AArch64/RISCV 的 `$d`/`$t` 映射符号要剥离；同一函数的多个入口要合并；以及和 FDE 的边界冲突。

#### 4.4.2 核心流程

```
discoverFileObjects()
 ├─ 第一遍扫符号：
 │    ├─ 拒绝 asan/coverage 二进制（直接 exit）
 │    └─ 收集所有 ST_File 符号 → FileSymbols（用于消歧）
 ├─ 第二遍扫符号，筛出「在内存里」的符号 → SortedSymbols
 │    （跳过 ST_File、未定义、非可分配 section 的）
 ├─ 校验符号地址不越出其 section（处理 AArch64 $d/$t 串进 .text 的情况）
 ├─ 按地址排序（stable_sort + 自定义比较：地址→marker→函数类型优先）
 ├─ （AArch64/RISCV）补全数据标记、剥离 marker 符号
 └─ 第三遍遍历 SortedSymbols，逐个处理：
      ├─ 跳过 ST_File / PLT stub / 0 地址
      ├─ 给局部符号消歧：拼 primary(<函数>/<id>) 与 alternative(<函数>/<file>/<id2>)
      ├─ registerNameAtAddress → 创建/复用 BinaryData
      ├─ 校验是否在代码段、是否落在别的函数内部（多入口/局部符号）
      ├─ 用 FDE 校验边界，冲突则 IsSimple=false
      ├─ 若该地址已有函数 → 加别名；否则 createBinaryFunction
      └─ 处理冷片段（.text.cold）符号
```

#### 4.4.3 源码精读

入口与第一遍扫描——拒绝带 sanitizer/coverage 的二进制，并收集 file 符号：

[lib/Rewrite/RewriteInstance.cpp:868-894](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L868-L894) —— 一进来就用 `NamedRegionTimer` 计时；然后遍历符号，凡是名字以 `__asan_init` 或 `__llvm_coverage_mapping` 开头的，直接 `exit(1)`（BOLT 不支持处理带插桩的二进制）；凡是类型为 `ST_File` 的，收进 `FileSymbols` 容器。注释点明了 file 符号的用途：**「为局部符号保留关联的 FILE 符号名，用组合名消歧」**。

筛选与排序——把「在内存里」的符号挑出来按地址排好：

[lib/Rewrite/RewriteInstance.cpp:945-971](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L945-L971) —— `isSymbolInMemory` 判定符号是否落在可分配 section（用临时构造的 `BinarySection` 调 `isAllocatable()`，正是 4.2 节那个谓词）；`checkSymbolInSection` 滤掉地址越出所属 section 的符号（典型是 AArch64 的 `$d`/`$t` marker 串进 `.text`）。`CompareSymbols` 是个多级比较器：先按地址，再让 marker 排前，再让 `STT_Function` 优先于非函数、让 `STT_Debug` 优先（处理 lld/GNU ld 的 section marker）。

**本讲核心：FILE 符号消歧**——给同名局部符号拼出 primary 和 alternative 两个名字：

[lib/Rewrite/RewriteInstance.cpp:1104-1124](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1104-L1124) —— 注释把策略讲得很清楚：局部符号（非 `SF_Global`）可能和某个全局符号重名，所以**一律改名**。对函数类型的局部符号，用 `upper_bound` 在已排序的 `FileSymbols` 里找到它前面最近的那个 file 符号，拿到源文件名，拼出：
- `primary`：`<函数名>/<id>`（`NR.uniquify` 保证唯一）
- `alternative`：`<函数名>/<文件名>/<id2>`

之所以要两个名字，是因为 perf profile 可能采集自一个被 strip 过调试信息的二进制（那时没有文件名信息），profile 里只有 `<函数>/<id>`；也可能采集自带文件名的二进制，profile 里是 `<函数>/<文件>/<行>`。BOLT 把两种形式都注册进去，匹配 profile 时就两条路都通——这正是 u4-l1 fdata 格式与本阶段的呼应。

最终创建函数——把符号落地为 `BinaryFunction`：

[lib/Rewrite/RewriteInstance.cpp:1316-1319](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1316-L1319) —— 若该地址还没有函数，就调 `BC->createBinaryFunction(名字, section, 地址, 大小)` 真正造出来；前面若 FDE 校验发现边界冲突（`IsSimple=false`），就把新函数标成非 simple（u2-l2 讲过，非 simple 函数只搬运不优化）。

`createBinaryFunction` 的实现——一行 `emplace` 把函数塞进按地址排序的 `BinaryFunctions` 映射：

[lib/Core/BinaryContext.cpp:902-913](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp#L902-L913) —— `BinaryFunctions.emplace(地址, BinaryFunction(...))`（这就是 u2-l1 那个值语义有序 map）；随后 `registerNameAtAddress` 给这个名字建 `BinaryData`，`setSymbolToFunctionMap` 建立「符号 → 函数」的反查索引。

`registerNameAtAddress` —— 顺带看看数据符号是怎么变成 `BinaryData` 的：

[lib/Core/BinaryContext.cpp:1184-1212](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp#L1184-L1212) —— 在 MC 层建一个 `MCSymbol`；若该地址还没有 `BinaryData`，就 `new BinaryData(...)` 并塞进 `BinaryDataMap`（按地址）和 `GlobalSymbols`（按名字）；若已有（多个符号指向同一地址），就把新符号挂到现有 `BinaryData` 的 `Symbols` 列表里。`BinaryData` 就是 u2-l1 提到的「按地址查符号数据」索引的条目。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：搞清 `discoverFileObjects` 如何用 FILE 符号区分同名局部函数，并列出 `readSpecialSections` 至少 3 类特殊 section。

**操作步骤**（源码阅读型，本讲指定任务）：

1. 打开 [lib/Rewrite/RewriteInstance.cpp:868-894](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L868-L894)，确认 `FileSymbols` 是在第一遍扫描里、靠 `Symbol.getType() == SymbolRef::ST_File` 收集的。
2. 跳到 [lib/Rewrite/RewriteInstance.cpp:1116-1121](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1116-L1121)，看 `upper_bound(FileSymbols, ...)` 如何找到「当前符号前面最近的 file 符号」——`upper_bound` 返回第一个大于当前符号的位置，`SFI[-1]` 就是它前面的 file 符号。
3. 写一段话回答：**为什么 local 函数要拼 `<函数>/<文件>` 的 alternative 名字，而 global 函数不用？**
4. 回到 4.3 节，列出 `readSpecialSections` 识别的至少 3 类特殊 section 及用途（`.eh_frame` / `.rela.text` / `.symtab` / `.note.bolt_bat` / `.debug_*`）。

**需要观察的现象**：

- `FileSymbols` 只装 `ST_File` 符号；它们本身不会被当成函数（第三遍里 [1043-1044 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1043-L1044) 直接 `continue`）。
- global 符号走 [1089-1103 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1089-L1103) 的分支，`UniqueName = Name` 不改名，并且会检查重名全局符号是否冲突（冲突则 `exit(1)`）。

**预期结果**（参考答案）：

> global 符号按定义在全程序内唯一，名字本身就够用；local 符号是文件作用域，多个源文件里可以有同名 local 函数（如多个 `.c` 里都有 `static int helper(void)`），名字撞车。BOLT 借 ELF 的 file 符号拿到「来自哪个源文件」，拼成 `<函数>/<文件>/<id>` 来区分。同时保留不带文件名的 primary 名字，是为了兼容从「被 strip 过调试信息」的二进制上采到的 profile。

`readSpecialSections` 至少 3 类特殊 section（详见 4.3.4 的表）：`.eh_frame`（异常处理/函数边界）、`.rela.text`（重定位，决定能否重排）、`.symtab`（符号表，决定是否 stripped）、`.note.bolt_bat`（BAT 地址翻译）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `discoverFileObjects` 要先扫一遍收集 `FileSymbols`，再扫一遍处理符号？能不能一遍搞定？

> **答案**：不能。因为 file 符号在符号表里出现在它「管辖的」那批 local 符号**之前**，要拿到「当前 local 符号属于哪个文件」，需要能在已排序的 file 符号集合里做 `upper_bound` 前溯查找。第一遍先建好这个有序集合，第二遍才能高效查询。两遍扫描是「先建索引再查询」的标准手法。

**练习 2**：`CompareSymbols` 里为什么让 `ST_Function` 类型排在同地址的其他符号前面？

> **答案**：同一个地址可能同时挂一个函数符号和一个数据/调试 marker 符号。让函数类型优先，保证后续遍历时**先**把该地址认作函数入口，其它符号再被当作「函数内部的局部符号」处理（[1185-1206 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1185-L1206)）。否则可能误把一个函数入口当成数据。

**练习 3**：`createBinaryFunction` 里 `BinaryFunctions.emplace(地址, ...)` 用「地址」作 key，意味着什么？

> **答案**：`BinaryFunctions` 是按地址排序的 `std::map`（u2-l1 讲过），key 是函数起始地址。这意味着**同一地址不能有两个函数**（断言 `unexpected duplicate function`），但一个函数可以有多个名字/入口（靠 `addAlternativeName` 和 `addEntryPointAtOffset`）。这对应了「一个地址 → 一个函数对象」的模型。

---

## 5. 综合实践

把本讲三个阶段串起来，完成一个「**给 BOLT 喂一个最小输入，追踪它的发现全过程**」的任务。

**任务**：准备一个最简单的多文件 C 程序，刻意制造同名 `static` 函数，观察 BOLT 如何消歧。

**步骤**：

1. 写两个源文件，各含一个同名 `static` 函数：
   ```c
   // a.c
   static int helper(void) { return 1; }
   int fa(void) { return helper(); }
   // b.c
   static int helper(void) { return 2; }
   int fb(void) { return helper(); }
   // main.c
   int fa(void); int fb(void);
   int main(void) { return fa() + fb(); }
   ```
   （以上为**示例代码**，非项目原有源码。）

2. 按 u1-l3 的方法链接（带 `-Wl,-q`）：
   ```bash
   clang -O2 -c a.c b.c main.c
   clang -Wl,-q a.o b.o main.o -o demo
   ```

3. 用 `llvm-readelf -s demo | grep helper` 观察两个 `helper` 符号——它们名字相同、都是 LOCAL、绑定到不同文件。

4. 用 `llvm-bolt demo -o demo.bolt -print-disasm -print-only=helper 2>&1 | grep -i helper`（或加 `-v`）观察 BOLT 内部给它们起的消歧名（应能看到 `helper/<文件名>/<id>` 形式）。

5. 对照源码解释：这两个 `helper` 在 [lib/Rewrite/RewriteInstance.cpp:1104-1124](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1104-L1124) 走的是 local 分支，各自被 `upper_bound` 匹配到不同的 file 符号（`a.c` 和 `b.c`），从而拼出不同的 alternative 名字，最终在 [lib/Core/BinaryContext.cpp:902-913](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryContext.cpp#L902-L913) 成为两个独立的 `BinaryFunction`。

**预期结果**：两个同名 `helper` 被识别成两个不同函数，各自带「/文件名/」的消歧名。若本地无 clang/llvm-bolt 环境，标注「待本地验证」，改为纯源码追踪：从 [868 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L868) 到 [1320 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Rewrite/RewriteInstance.cpp#L1320) 画出「符号 → BinaryFunction」的状态流转图。

## 6. 本讲小结

- `run()` 的发现阶段是 **`discoverStorage` → `readSpecialSections` → `discoverFileObjects`** 三步走：先建地址空间底图，再注册所有 section，最后才扫符号表认函数——顺序由「依赖关系」决定。
- `discoverStorage` 算出 `NextAvailableAddress`（新代码起点）、记录段信息、并**拒绝处理已被 BOLT 处理过的二进制**（检测 `.bolt.org` 前缀）。
- `BinarySection` 是「输入态 + 输出态合一」的 section 抽象，被 `BinaryContext` 同时索引在「按地址 / 按名字 / 按 SectionRef」三套表里；只有可分配且有地址的 section 才进地址索引。
- `readSpecialSections` 注册全部 section 并探测关键特殊 section：`.rela.text`（决定 `HasRelocations`，即能否重排函数）、`.symtab`（决定 `IsStripped`）、`.eh_frame`（构造 `CFIReaderWriter` 供函数边界校验）、`.note.bolt_bat`（BAT）。
- `discoverFileObjects` 遍历符号表，把函数符号变成 `BinaryFunction`、数据符号变成 `BinaryData`；用 **`ST_File` 符号**给同名 local 函数拼出 `<函数>/<文件>/<id>` 的消歧名，并兼容不带文件名的 primary 名以匹配 profile。
- 函数边界还会用 `.eh_frame` 的 FDE 交叉校验，发现冲突就把函数标记为非 simple（只搬运不优化）。

## 7. 下一步学习建议

本讲只走到「认出函数对象」为止，**还没有反汇编、没有建 CFG**。下一讲 **u3-l3《反汇编与 CFG 重建：processIndirectBranch 与启发式》** 会接着 `disassembleFunctions()` / `buildFunctionsCFG()`，讲 BOLT 如何把每个 `BinaryFunction` 的字节流解码成指令、切成基本块、连成控制流图，并重点剖析间接分支（跳转表、间接跳转）的启发式判定——那是发现阶段之后最关键的一步。

建议同步阅读的源码：

- [lib/Core/BinaryFunction.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryFunction.cpp) 中的 `disassemble()` 与 `buildCFG()`（u3-l3 主题）。
- [lib/Core/JumpTable.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/JumpTable.cpp)（跳转表识别，u3-l3/u3-l4）。
- 复习 [include/bolt/Core/BinarySection.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/include/bolt/Core/BinarySection.h) 与 [lib/Core/BinaryData.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/bolt/lib/Core/BinaryData.cpp)，因为后续重定位（u3-l4）会大量操作这俩对象。
