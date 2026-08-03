# 重定位扫描与处理

## 1. 本讲目标

本讲是「重定位与目标架构」单元的核心篇。读完本讲，你应当能够：

- 说清 LLD 的重定位处理为什么分成 **扫描（scan）** 和 **后扫描（postScan）** 两个阶段，各自做什么、为什么不能合并；
- 读懂 `RelocScan::process` / `processAux` 这条主决策链：它如何根据 `RelExpr` 决定 **是否需要 GOT/PLT 槽位**、**是否在链接期算完**、**是否要留给动态链接器**；
- 理解 `isStaticLinkTimeConstant` 如何判定一条重定位是「链接期常量」还是「运行期动态重定位」，以及这一区分对输出体积的直接影响；
- 读懂 copy relocation 与 canonical PLT 的 **就地符号替换** 机制（`replaceWithDefined`），并说清本次代码更新（`fetch_and` 取代整 `Symbol` 拷贝）的来龙去脉。

> 本次为 **update** 讲义。`prev HEAD → cur HEAD` 之间，`lld/` 仅 `ELF/Relocations.cpp` 一处 NFC 改动：`replaceWithDefined` 不再整体拷贝 `Symbol`，而是只暂存 `versionId` 一个成员，并用原子的 `flags.fetch_and(NEEDS_GOT)` 取代「从快照 load 再 store」。本讲据此重写「就地符号替换」一节，并刷新全部永久链接行号。

## 2. 前置知识

在进入本讲前，建议你已经建立以下认知（见前置讲义）：

- **Symbol 与 placement new 就地解析**（u4-l1）：LLD 把所有符号种类（`Defined`/`Undefined`/`SharedSymbol`/`LazyArchive` 等）放进**同一大小的内存槽**里，用一个 `Symbol &` 始终指向它。「解析一个符号」就是用 `overwrite` 把槽里的字节原地改写，于是所有旧指针自动指向新结果。
- **TargetInfo 与 RelExpr**（u6-l1）：每条机器相关的重定位类型（如 `R_X86_64_PC32`）先被 `TargetInfo::getRelExpr` 翻译成 **架构无关的 `RelExpr` 语义**（如 `R_PC`），后续决策都基于 `RelExpr`。
- **Ctx 与并行**（u2-l1、u9-l2）：LLD 大量使用 `parallelFor`，符号上的标志位是 `std::atomic`，写真实槽位的「分配阶段」则是串行的。
- **诊断流与检查点**（u3-l2）：错误用 `Err(ctx)` 报告、用 `errCount` 在阶段边界提前返回。

如果你对「重定位（relocation）」「GOT / PLT」「动态链接器」「位置无关代码（PIC）」这些链接器基础概念还不熟悉，建议先补一下 ELF 动态链接入门，再回头读本讲。

## 3. 本讲源码地图

本讲聚焦两个文件，并附带三个支撑文件：

| 文件 | 作用 |
| --- | --- |
| [ELF/RelocScan.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/RelocScan.h) | `RelocScan` 类：单条重定位的扫描状态机，含 `scan` / `process` / `processAux` / `isStaticLinkTimeConstant` 等成员，以及若干内联热点（`processR_PC`、`processR_PLT_PC`）。 |
| [ELF/Relocations.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp) | 重定位处理的「实现全集」：入口 `scanRelocations`、后扫描 `postScanRelocations`、copy reloc `addCopyRelSymbol`、就地替换 `replaceWithDefined`、`RelExpr` 分类辅助函数。 |
| [ELF/Relocations.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.h) | `RelExpr` 枚举、`Relocation` 结构体定义。 |
| [ELF/Symbols.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Symbols.h) | `Symbol` 基类、`overwrite` 机制、`flags` / `versionId` 字段、`NEEDS_*` 标志枚举。 |
| [ELF/Symbols.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Symbols.cpp) | `Defined::overwrite` 的实现，是理解 `replaceWithDefined` 为何要「回填 versionId」的关键。 |

一句话定位：`RelocScan.h` 是 **决策骨架**，`Relocations.cpp` 是 **决策实现 + 副作用落地**，`Symbols.*` 解释 **为什么符号能被就地改写**。

## 4. 核心概念与源码讲解

### 4.1 重定位扫描阶段：从 scanRelocations 到 process

#### 4.1.1 概念说明

目标文件里的每一条重定位，本质上是一句「请在此处填入符号 S 的地址（加上 addend）」。但「填入」这件事在 LLD 里被刻意拆成两步：

1. **扫描（scan）**：只 **规划**，不真正写值。逐条重定位回答三个问题：
   - 它需不需要 **GOT 槽位**（间接取地址）？
   - 它需不需要 **PLT 槽位**（间接调用）？
   - 它能不能在 **链接期就算完**（写死进 `.text`），还是必须留给 **动态链接器** 在加载时填（即生成一条 `.rela.dyn` 条目）？
   
   扫描把答案以 **原子标志位** 的形式记在符号上（`NEEDS_GOT` / `NEEDS_PLT` / `NEEDS_COPY` 等），并把它归类结果记在 `InputSectionBase::relocations` 里，供后面写盘时 `relocateAlloc` 真正填值。

2. **后扫描（postScan）**：串行地 **消费** 这些标志位，真正分配 GOT/PLT 槽位、生成 copy relocation、把 `SharedSymbol` 就地改成 `Defined`。

为什么要先扫描、后落地？`Relocations.cpp` 顶部注释给出了根本原因（见 [ELF/Relocations.cpp:908-920](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L908-L920)）：LLD 用 `mmap` 一次性映射输出文件，**必须先知道输出文件多大**；而文件大小取决于动态重定位、GOT、PLT 各自有多少条——这只有扫完所有重定位才能确定。所以扫描是一次「预估体积」的规划遍。

#### 4.1.2 核心流程

扫描阶段从 `scanRelocations` 入口到单条重定位的处理，是一条清晰的调用链：

```
elf::scanRelocations(ctx)                         // 并行总入口
  └─ parallelFor(numWorkers, shard =>             // 多 worker 抢占式 claim 文件
       for each InputSection in objectFiles[i]:
         ctx.target->scanSection(sec, shard)      // invokeELFT 选 ELFT 实例
           └─ scanSection1<ELFT>                  // 按 REL/RELA/CREL 分派
                └─ scanSectionImpl<ELFT,RelTy>    // 建 RelocScan，遍历重定位
                     └─ for each reloc:
                          RelocScan::scan(...)    // 取 sym、算 expr
                            └─ RelocScan::process(expr, type, off, sym, addend)
                                 ├─ 非抢占/非 ifunc：fromPlt 优化掉 PLT
                                 ├─ needsGot(expr) → setFlags(NEEDS_GOT)
                                 ├─ needsPlt(expr) → setFlags(NEEDS_PLT)
                                 └─ processAux(...)   // 决策下半段（见 4.2）
```

几个关键设计点：

- **并行 + 分片（shard）**：每个 worker 持有一个 `shard` 编号，它发现的动态重定位被追加到各自的 `relocsVec` 分片里，避免对全局 `.rela.dyn` 加锁。MIPS 是个例外——它在扫描期会改 `MipsGotSection`，故强制单 worker（见 [ELF/Relocations.cpp:1183-1187](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L1183-L1187)）。
- **惰性**：注释明确说「本文件只分析需要做什么，不真正施加重定位——那发生在后面的 `InputSection::writeTo()`」（[ELF/Relocations.cpp:17-21](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L17-L21)）。这正是 NewLLD「少做事」哲学的体现。
- **`scan` 还顺手做了未定义符号检查**：符号若是 `Undefined` 且非 marker 重定位，调用 `maybeReportUndefined`（但只是把错误收集进 `ctx.undefErrs`，扫描结束后再统一打印，避免并行乱序输出）。

#### 4.1.3 源码精读

**总入口 `scanRelocations`**——并行分发，每个 worker 用原子计数器 `next` 抢文件来扫（[ELF/Relocations.cpp:1175-1211](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L1175-L1211)）：

```cpp
size_t numWorkers = ctx.arg.emachine == EM_MIPS
                        ? 1
                        : std::min<size_t>(ctx.arg.threadCount, numFiles + 1);
parallelFor(0, numWorkers, [&](unsigned shard) {
  for (size_t i;
       (i = next.fetch_add(1, std::memory_order_relaxed)) <= numFiles;) {
    if (i != numFiles) {
      for (InputSectionBase *s : ctx.objectFiles[i]->getSections())
        if (s && s->kind() == SectionBase::Regular && s->isLive() &&
            (s->flags & SHF_ALLOC) && ...)
          ctx.target->scanSection(*s, shard);
      continue;
    }
    // 最后一个 worker 扫描 .eh_frame / ARM exidx 等特殊段
    ...
  }
});
```

注意它只扫 `SHF_ALLOC` 且 `isLive()` 的段——GC 掉的死段、非分配段（如 `.debug_*`）不走这条主链。

**`RelocScan` 类**——单条重定位的状态机，持有 `ctx`、当前段 `sec` 和分片号 `shard`（[ELF/RelocScan.h:48-58](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/RelocScan.h#L48-L58)）。`scan` 是逐条重定位的入口：取符号、用 `target->getRelExpr` 把机器类型翻译成 `RelExpr`，再交给 `process`（[ELF/RelocScan.h:188-205](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/RelocScan.h#L188-L205)）：

```cpp
template <class ELFT, class RelTy>
void RelocScan::scan(typename Relocs<RelTy>::const_iterator &it, RelType type,
                     int64_t addend) {
  const RelTy &rel = *it;
  Symbol &sym = sec->getFile<ELFT>()->getSymbol(rel.getSymbol(false));
  RelExpr expr =
      ctx.target->getRelExpr(type, sym, sec->content().data() + rel.r_offset);
  if (sym.isUndefined() && symIdx != 0 &&
      maybeReportUndefined(cast<Undefined>(sym), offset))
    return;
  process(expr, type, offset, sym, addend);
}
```

**`process` 的上半段**——做 PLT/GOT 的「优化与归类」，决定打哪些标志位（[ELF/Relocations.cpp:921-970](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L921-L970)）。这是本讲实践任务要找的「触发 GOT/PLT 分配的分支」：

```cpp
void RelocScan::process(RelExpr expr, RelType type, uint64_t offset,
                        Symbol &sym, int64_t addend) const {
  const bool isIfunc = sym.isGnuIFunc();
  // 非抢占、非 ifunc：能直接调用就别走 PLT
  if (!sym.isPreemptible && !isIfunc) {
    if (expr != R_GOT_PC)
      expr = fromPlt(expr);          // R_PLT_PC → R_PC，省一个 PLT 槽
    ...
  }
  ...
  if (needsGot(expr)) {              // ★ 需要 GOT 的分支
    ...
    sym.setFlags(NEEDS_GOT | NEEDS_GOT_NONAUTH);
  } else if (needsPlt(expr)) {       // ★ 需要 PLT 的分支
    sym.setFlags(NEEDS_PLT);
  } else if (LLVM_UNLIKELY(isIfunc)) {
    sym.setFlags(HAS_DIRECT_RELOC);
  }
  processAux(expr, type, offset, sym, addend);   // 进入下半段（4.2）
}
```

注意：`process` **只打标志位，不分配槽位**。真正 `addGotEntry` / `addPltEntry` 要等到后扫描阶段（见 4.3）。标志位是 `std::atomic<uint16_t>`（[ELF/Symbols.h:303-305](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Symbols.h#L303-L305)），所以多 worker 并行 `setFlags` 是安全的。

> 小贴士：`needsGot` / `needsPlt` 都用 `oneof<...>` 把一组 `RelExpr` 压成一个 128 位常量掩码做成员判定（[ELF/RelocScan.h:34-46](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/RelocScan.h#L34-L46)），编译期展开成几次移位与按位与，是非常热路径上的小优化。

#### 4.1.4 代码实践

**实践目标**：在源码里定位「扫描阶段只打标志、不落地」这一设计，并用 `readelf` 在真实产物上看到扫描规划出的 GOT/PLT/动态重定位。

**操作步骤**：

1. 在 [ELF/Relocations.cpp:921-970](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L921-L970) 的 `process` 中，找到 `needsGot(expr)` 与 `needsPlt(expr)` 两个分支，确认它们只调用了 `sym.setFlags(...)`，没有任何 `ctx.in.got->addEntry(...)`。
2. 再到后扫描 [ELF/Relocations.cpp:1296-1311](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L1296-L1311) 的 `postScanRelocations` 中，确认 `addGotEntry` / `addPltEntry` 才出现在这里——验证「打标志」与「分配槽位」确实分属两个阶段。
3. 写一个最小程序引用 `printf`（来自 libc.so）：

   ```c
   /* 示例代码：demo.c */
   #include <stdio.h>
   int main(void) { printf("hi\n"); return 0; }
   ```

4. 用 `clang -fuse-ld=lld -o demo demo.c` 链接，再执行：

   ```bash
   readelf -r demo | head        # 查看动态重定位
   readelf -S demo | grep -E "got|plt|rela"   # 查看 GOT/PLT/rela 段
   ```

**需要观察的现象**：

- `.rela.plt` 里应有一条针对 `printf` 的 `R_X86_64_JUMP_SLOT`；`.got.plt` 里有对应槽位。
- `.rela.dyn` 里通常还有若干 `R_X86_64_RELATIVE`（由 PIE 引起）。

**预期结果**：你能把 `readelf` 看到的每一条动态重定位，对应回扫描阶段在 `processAux` 中「判定为非常量 → 写入 `ctx.in.relaDyn`」的代码路径。若你身处无法编译的环境，明确写 **待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`scanRelocations` 为什么只扫 `SHF_ALLOC` 的段？`.debug_info` 里的重定位由谁处理？
**答案**：因为只有分配段才会进最终镜像、影响体积与运行期行为；调试段等非分配段的重定位由 `InputSection::relocateNonAlloc` 在写盘时直接就地处理（见文件头注释 [ELF/Relocations.cpp:17-21](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L17-L21)），不需要提前规划体积。

**练习 2**：为什么 MIPS 在扫描时强制 `numWorkers = 1`？
**答案**：MIPS ABI 在扫描期就要修改 `MipsGotSection`（GOT 的填充逻辑与常规 ABI 相反），这不是线程安全的，故退化为单线程（[ELF/Relocations.cpp:1183-1187](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L1183-L1187)）。

---

### 4.2 RelExpr 决策与链接期常量判定

#### 4.2.1 概念说明

`process` 的下半段 `processAux` 面对每条重定位要回答的核心问题是：**这条重定位的最终值，链接时就能算出来吗？**

- **能** → 称为 **链接期常量（link-time constant）**。LLD 把它记进 `sec->relocations`，等写盘时由 `relocateAlloc` 直接把算好的字节 patch 进 `.text`。**不产生任何额外输出。**
- **不能** → 必须生成一条 **动态重定位**，写进 `.rela.dyn`（或 `.rela.plt`），由动态链接器在加载/运行期填值。每条 `Elf64_Rela` 占 24 字节，往往还要附带一个 8 字节的 GOT 槽或 16 字节的 PLT 槽，**直接增大输出体积**。

这条分界由 `isStaticLinkTimeConstant` 判定。决定它难不难的关键在于 **PIC（位置无关）** 与 **符号可抢占性（preemptible）**：在共享库或 PIE 里，符号的最终地址在加载时才确定，许多重定位就「链接期算不完」。

#### 4.2.2 核心流程

`processAux` 的决策是一棵优先级清晰的判定树（[ELF/Relocations.cpp:975-1124](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L975-L1124)）：

```
processAux(expr, type, off, sym, addend):
  if isStaticLinkTimeConstant(...):          // ① 链接期常量
     sec->addReloc({expr, type, off, addend, &sym});   // 写盘时 patch，无动态 reloc
     return
  if canWrite:                               // ② 段可写：用相对/符号动态重定位
     if GOT 类 或 非抢占符号重定位: addRelativeReloc(...)
     elif getDynRel(type) != 0: relaDyn->addSymbolReloc(...)
     return
  if !shared 且 sym 来自 DSO:                // ③ 可执行文件直接引用 DSO 符号
     if sym.isObject(): setFlags(NEEDS_COPY); ...   // copy reloc 候选（4.3）
     elif sym.isFunc():  setFlags(NEEDS_COPY|NEEDS_PLT); ... // canonical PLT 候选
     return
  // ④ 都不满足：报错 "relocation cannot be used; recompile with -fPIC"
```

而 `isStaticLinkTimeConstant` 内部的判定逻辑（[ELF/Relocations.cpp:841-906](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L841-L906)）大致是：

1. **GOT/PLT 类表达式恒为常量**——因为它们引用的是「GOT 槽的地址」或「PLT 桩的地址」，这些槽/桩本身的地址在链接期可定（值通过 GOT 间接拿，是运行期的事，但重定位本身链接期可算）。
2. **可抢占符号**：通常不是常量；唯一例外是未定义弱符号在非 PIC 下。
3. **非 PIC 模式**：一切皆常量（绝对地址链接）。
4. **PIC 模式下的相对/绝对分类**：用 `isAbsoluteOrTls(sym)` 与 `isRelExpr(e)` 的组合判断「绝对值配相对表达式」「相对值配绝对表达式」是否成立。

可以把它抽象成一个布尔判定：在 PIC 下，一条重定位是常量当且仅当「表达式的绝对/相对性」与「符号的绝对/相对性」匹配（同则常量），即

\[
\text{constant} \;\iff\; \text{isAbsoluteOrTls}(sym) \oplus \text{isRelExpr}(e) \quad\text{（在 PIC、非抢占前提下）}
\]

其中 \(\oplus\) 为异或——两者「一绝对一相对」时才能在链接期算出与位置无关的正确值。

#### 4.2.3 源码精读

**`RelExpr` 枚举**——架构无关的语义分类，把上百种机器重定位归约成约 60 个语义（[ELF/Relocations.h:42-119](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.h#L42-L119)）。`R_*` 是通用语义，`RE_<TARGET>_*` 是某架构专有语义：

```cpp
enum RelExpr {
  R_ABS,       // 绝对地址
  R_PC,        // PC 相对：S + A - P
  R_PLT_PC,    // 经 PLT 的 PC 相对：L + A - P
  R_GOT,       // GOT 间接：G + A （G 为 GOT 槽地址）
  R_GOT_PC,    // GOT 槽的 PC 相对地址
  ...
  RE_AARCH64_GOT_PAGE,   // AArch64 专有：页对齐的 GOT
  RE_RISCV_ADD,
  ...
};
```

**分类辅助函数**——把 `RelExpr` 归类为「需要 GOT」「需要 PLT」「相对表达式」等（[ELF/Relocations.cpp:127-152](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L127-L152)）：

```cpp
bool lld::elf::needsGot(RelExpr expr) {
  return oneof<R_GOT, R_GOT_OFF, ..., R_GOT_PC, R_GOTPLT, ...>(expr);
}
static bool needsPlt(RelExpr expr) {
  return oneof<R_PLT, R_PLT_PC, R_PLT_GOTREL, R_PLT_GOTPLT, ...>(expr);
}
// True if this expression is of the form Sym - X (PC/GOT 相对)
static bool isRelExpr(RelExpr expr) {
  return oneof<R_PC, R_GOTREL, R_GOTPLTREL, ...>(expr);
}
```

**`isStaticLinkTimeConstant` 的核心**（[ELF/Relocations.cpp:867-885](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L867-L885)）：

```cpp
if (sym.isPreemptible)
  return sym.isUndefined() && !ctx.arg.isPic;
if (!ctx.arg.isPic)
  return true;                       // 非 PIC：链接期总能算
...
bool absVal = isAbsoluteOrTls(sym) && e != RE_PPC64_TOCBASE;
bool relE = isRelExpr(e);
if (absVal && !relE)  return true;   // 绝对值 + 绝对表达式
if (!absVal && relE)  return true;   // 相对值 + 相对表达式
if (!absVal && !relE) return ctx.target->usesOnlyLowPageBits(type);
// assert(absVal && relE);          // 绝对值 + 相对表达式：通常非常量
```

**`processAux` 落地动态重定位**（[ELF/Relocations.cpp:995-1045](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L995-L1045)）——当判定为非常量且段可写时，生成相对或符号动态重定位：

```cpp
if (canWrite) {
  RelType rel = ctx.target->getDynRel(type);
  if (oneof<R_GOT, RE_LOONGARCH_GOT>(expr) ||
      ((rel == ctx.target->symbolicRel || ...) && !sym.isPreemptible)) {
    addRelativeReloc<true>(ctx, *sec, offset, sym, addend, expr, type, shard);
    return;
  }
  if (rel != 0) {
    std::lock_guard<std::mutex> lock(ctx.relocMutex);   // 写全局 relaDyn 要加锁
    ...
    ctx.in.relaDyn->addSymbolReloc(rel, *sec, offset, sym, addend, type);
    ...
  }
}
```

注意这里用 `ctx.relocMutex` 保护对全局 `.rela.dyn` 的追加——这是并行扫描中少数需要细粒度锁的地方。

#### 4.2.4 代码实践

**实践目标**：直观感受「链接期常量 vs 运行期动态重定位」对输出体积的影响。

**操作步骤**：

1. 用同一段 `demo.c` 分别生成非 PIE 可执行文件与 PIE：

   ```bash
   clang -fuse-ld=lld -no-pie -o demo_nopie demo.c
   clang -fuse-ld=lld -pie    -o demo_pie   demo.c
   ```

2. 对比两者的动态重定位数量：

   ```bash
   readelf -r demo_nopie | tail -n +3 | wc -l
   readelf -r demo_pie   | tail -n +3 | wc -l
   ls -l demo_nopie demo_pie
   ```

**需要观察的现象**：

- `-no-pie` 下绝大多数重定位是链接期常量，`.rela.dyn` 条目很少；
- `-pie` 下因 PIC 约束，多出大量 `R_X86_64_RELATIVE`，文件体积也更大。

**预期结果**：PIE 产物的 `.rela.dyn` 条目数与体积都显著多于非 PIE，印证了 `isStaticLinkTimeConstant` 中 `!ctx.arg.isPic → return true` 那条短路（[ELF/Relocations.cpp:869-870](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L869-L870)）——非 PIC 能省掉大量动态重定位。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么「GOT 类表达式（`R_GOT`/`R_GOT_PC` 等）总是链接期常量」，哪怕符号是可抢占的？
**答案**：因为这些重定位引用的是「GOT 槽本身的地址」，而 GOT 槽在输出文件里的位置链接期即可确定；真正随加载地址变化的符号值，是动态链接器在运行期写进 GOT 槽的内容，那不属于本条重定位要 patch 的值。

**练习 2**：`processAux` 里 `canWrite` 分支中，`addRelativeReloc` 与 `relaDyn->addSymbolReloc` 的区别是什么？
**答案**：`R_X86_64_RELATIVE` 这类相对重定位不带符号（只含 `S+A` 的偏移），体积小且无需进 `.dynsym`；而 `addSymbolReloc` 生成的是带符号的动态重定位（如 `R_X86_64_64` 对可抢占符号），需要把符号也写进 `.dynsym`，开销更大。LLD 优先用相对重定位以减小输出。

---

### 4.3 copy relocation 与 canonical PLT 的就地符号替换

#### 4.3.1 概念说明

当一个 **非 PIC 的可执行文件** 直接引用 DSO 里的符号时，链接器无法把指令改成「走 GOT 间接寻址」（指令已经在目标文件里写死了），于是有两种「打补丁」手段：

- **copy relocation（拷贝重定位）**：用于 **数据符号**（`STT_OBJECT`）。LLD 在 `.bss`（或只读数据用 `.bss.rel.ro`）里为该符号预留一块空间，生成一条 `R_*_COPY` 动态重定位，指示动态链接器在加载时把 DSO 里的数据 **拷贝** 进来。于是该符号在主程序里「变成」了一个本地定义。
- **canonical PLT（规范化 PLT）**：用于 **函数符号**（`STT_FUNC`）。LLD 为它建一个 PLT 桩，让该符号的「地址」指向这个桩，保证取地址与调用都指向同一处，维持指针相等性。

两者都有一步关键操作：把原本的 `SharedSymbol`（指向 DSO）**就地改写** 成一个 `Defined`（分别指向 `.bss` 或 `.plt`）。这就是 `replaceWithDefined` 的职责。

> **就地（in-place）** 二字回到 u4-l1：所有符号同处一个等大的内存槽，`Symbol &` 指针永远有效。改写槽内字节后，所有持有该符号指针的 `InputSection::relocations`、GOT、PLT 都自动看到新的 `Defined`——无需更新任何指针。这是 LLD 符号解析的核心技巧。

#### 4.3.2 核心流程

copy reloc / canonical PLT 的处理 **横跨扫描与后扫描两个阶段**：

```
【扫描期 processAux】 ③ 分支：
   发现 !shared && sym 来自 DSO 且非常量
     ├─ sym.isObject() → setFlags(NEEDS_COPY)            // 仅打标志
     └─ sym.isFunc()   → setFlags(NEEDS_COPY | NEEDS_PLT) // 仅打标志

【后扫描 postScanRelocations】串行消费 NEEDS_COPY：
   if (flags & NEEDS_COPY):
     ├─ sym.isObject():
     │     invokeELFT(addCopyRelSymbol, ctx, sym)        // ★ 真正建 .bss + COPY reloc
     │       └─ 为每个别名 replaceWithDefined(ctx, *sym, *bssSec, 0, size)
     └─ sym.isFunc() (canonical PLT):
           replaceWithDefined(ctx, sym, *ctx.in.plt, pltOffset, 0)  // ★ 指向 .plt
           sym.setFlags(NEEDS_COPY)
```

注意：**扫描期只打标志位 `NEEDS_COPY`**，真正分配空间、生成 COPY 重定位、改写符号都在后扫描里做。这种「先标记后处理」正是为了让扫描能并行、无副作用。

#### 4.3.3 源码精读

**`replaceWithDefined`——本次更新的主角**（[ELF/Relocations.cpp:244-255](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L244-L255)）：

```cpp
static void replaceWithDefined(Ctx &ctx, Symbol &sym, SectionBase &sec,
                               uint64_t value, uint64_t size) {
  uint16_t versionId = sym.versionId;                 // ① 只暂存这一个成员
  Defined(ctx, sym.file, StringRef(), sym.binding, sym.stOther, sym.type, value,
          size, &sec)
      .overwrite(sym);                                // ② 就地把 SharedSymbol 改写成 Defined

  sym.versionId = versionId;                          // ③ 回填 versionId
  sym.isUsedInRegularObj = true;
  // A copy relocated alias may need a GOT entry.
  sym.flags.fetch_and(NEEDS_GOT, std::memory_order_relaxed);  // ④ 只保留 NEEDS_GOT
}
```

**为什么必须回填 `versionId`？** 看 `Defined::overwrite` 的实现（[ELF/Symbols.cpp:711-719](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Symbols.cpp#L711-L719)）：

```cpp
void Defined::overwrite(Symbol &sym) const {
  if (isa_and_nonnull<SharedFile>(sym.file))
    sym.versionId = VER_NDX_GLOBAL;          // ★ 来自 DSO 的符号会被强制设成 GLOBAL
  Symbol::overwrite(sym, DefinedKind);
  auto &s = static_cast<Defined &>(sym);
  s.value = value;  s.size = size;  s.section = section;
}
```

被 copy reloc 的符号原本来自 DSO（`sym.file` 是 `SharedFile`），`Defined::overwrite` 会把它的 `versionId` **重置为 `VER_NDX_GLOBAL`**，丢掉原本的版本信息（比如 `foo@VERSION_1.2`）。但 copy reloc 必须为每个版本别名都生成独立的拷贝（不同别名地址不同，见 [ELF/Relocations.cpp:230-236](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L230-L236)），所以必须把真实 `versionId` **抢救回来**。

**本次更新：为什么从「整 Symbol 拷贝」改成「只存 versionId + fetch_and」？**

旧代码（`prev HEAD`）是这样的：

```cpp
// 旧代码（已不再存在）
Symbol old = sym;                                   // 整体拷贝整个 SymbolUnion
Defined(...).overwrite(sym);
sym.versionId = old.versionId;                      // 从快照回填
sym.isUsedInRegularObj = true;
sym.flags.store(old.flags.load(std::memory_order_relaxed) & NEEDS_GOT,
                std::memory_order_relaxed);         // 从快照读 flags，掩码后写回
```

注意 `Symbol` 的拷贝构造是 `memcpy` 整个等大联合体（[ELF/Symbols.h:81-83](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Symbols.h#L81-L83)）：

```cpp
// The default copy constructor is deleted due to atomic flags. Define one for
// places where no atomic is needed.
Symbol(const Symbol &o) { memcpy(static_cast<void *>(this), &o, sizeof(o)); }
```

也就是 `Symbol old = sym;` 会拷贝整个 `SymbolUnion`（数十字节，含 `Defined` 的 `value`/`size`/`section` 等所有变体字段），而真正用到的只有 `versionId` 一个 `uint16_t`。改动要点：

1. **只暂存真正会被破坏的成员**：`Defined::overwrite` 只会改写 `versionId`（对来自 DSO 的符号）和 `value`/`size`/`section`/`file`/`type`/`binding`/`stOther`/`symbolKind`（见基类 `Symbol::overwrite`，[ELF/Symbols.h:248-256](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Symbols.h#L248-L256)）。其中只有 `versionId` 是需要跨 `overwrite` 保留的（其余正是我们想覆盖的）。所以只存 `uint16_t versionId`（2 字节）即可，省去整个联合体的拷贝。

2. **`flags` 用 `fetch_and` 直接对活字段做原子读-改-写**：注意 `overwrite` 全程 **不碰 `flags`**（基类与 `Defined::overwrite` 都没写 `flags`），所以 `overwrite` 前后 `sym.flags` 的值不变。旧代码之所以从快照 `old.flags` 读，只是因为它已经顺手做了快照；既然现在不再做整符号快照，就直接对 `sym.flags` 做一次 `fetch_and(NEEDS_GOT)`——它原子地读出当前值、并写入 `当前值 & NEEDS_GOT`。最终状态与旧代码的 `store(old & NEEDS_GOT)` 完全一致。

   语义上 `fetch_and(NEEDS_GOT)` 保留 `NEEDS_GOT` 位、清掉其余所有位（`NEEDS_PLT`/`NEEDS_TLSDESC`/…）。理由：copy reloc 之后符号已是主程序里的本地定义，不再需要 PLT 或 TLS 间接，但它的某个别名可能仍通过 GOT 被引用，故保留 `NEEDS_GOT`。

**结论**：这是一次 **纯 NFC（无功能变化）微优化**——行为完全一致，但少了一次整个 `SymbolUnion` 的 `memcpy`，并把「load 快照 + store」收敛成一次原子 RMW。它之所以安全，关键在于 `overwrite` 不动 `flags`、且 `versionId` 是唯一被 `Defined::overwrite` 破坏又需要保留的字段。

**后扫描如何调用它**——`postScanRelocations` 消费 `NEEDS_COPY`（[ELF/Relocations.cpp:1312-1334](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L1312-L1334)）：

```cpp
if (flags & NEEDS_COPY) {
  if (sym.isObject()) {
    invokeELFT(addCopyRelSymbol, ctx, cast<SharedSymbol>(sym));  // copy reloc
    assert(!sym.hasFlag(NEEDS_COPY));   // addCopyRelSymbol 会清掉别名上的 NEEDS_COPY
  } else {
    assert(sym.isFunc() && sym.hasFlag(NEEDS_PLT));
    if (!sym.isDefined()) {
      replaceWithDefined(ctx, sym, *ctx.in.plt,              // canonical PLT
                         ctx.target->pltHeaderSize +
                             ctx.target->pltEntrySize * sym.getPltIdx(ctx), 0);
      sym.setFlags(NEEDS_COPY);
      ...
    }
  }
}
```

**`addCopyRelSymbol` 建 `.bss` 并对每个别名就地替换**（[ELF/Relocations.cpp:299-328](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L299-L328)）：

```cpp
template <class ELFT> static void addCopyRelSymbol(Ctx &ctx, SharedSymbol &ss) {
  bool isRO = isReadOnly<ELFT>(ss);
  BssSection *sec = make<BssSection>(ctx, isRO ? ".bss.rel.ro" : ".bss",
                                     symSize, ss.alignment);
  ...                                              // 把 sec 挂到对应 OutputSection
  for (SharedSymbol *sym : getSymbolsAt<ELFT>(ctx, ss))   // 遍历所有同地址别名
    replaceWithDefined(ctx, *sym, *sec, 0, sym->size);    // ★ 每个别名都就地改成指向 .bss
  ctx.in.relaDyn->addSymbolReloc(ctx.target->copyRel, *sec, 0, ss);  // R_*_COPY
}
```

`getSymbolsAt`（[ELF/Relocations.cpp:213-237](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L213-L237)）在 DSO 的 `.dynsym` 里找出所有与 `ss` 同地址的别名——这正是 [ELF/Relocations.cpp:257-298](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L257-L298) 那段长注释反复强调的「符号别名也是 ABI 的一部分」的体现。

#### 4.3.4 代码实践（本讲核心实践）

**实践目标**：亲手触发 copy relocation，在产物里看到它，并讲清 `replaceWithDefined` 的本次改动。

**操作步骤**：

1. 构造一个导出 **全局变量**（不是函数）的共享库——这正是会触发 copy reloc 的场景：

   ```c
   /* 示例代码：libvar.c */
   int shared_global = 42;     /* 导出变量，非函数 */
   ```

   ```c
   /* 示例代码：main.c（故意非 PIC 直接引用） */
   extern int shared_global;
   int main(void) { return shared_global; }
   ```

2. 编译链接：

   ```bash
   clang -fuse-ld=lld -shared -o libvar.so libvar.c
   clang -fuse-ld=lld -no-pie -o main main.c -L. -lvar
   ```

3. 查看产物：

   ```bash
   readelf -r main | grep COPY          # 应看到 R_X86_64_COPY
   readelf -S main | grep bss           # 应看到 .bss 或 .bss.rel.ro
   ```

4. **源码阅读**（必做）：打开 [ELF/Relocations.cpp:244-255](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L244-L255)，对照 [ELF/Symbols.cpp:711-719](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Symbols.cpp#L711-L719) 的 `Defined::overwrite`，回答下面的问题。

**需要观察并解释的现象（对应规格里的三问）**：

1. **`process` 中哪些分支会触发 GOT/PLT 分配？**
   答：`needsGot(expr)` 与 `needsPlt(expr)` 两个分支（[ELF/Relocations.cpp:948-967](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L948-L967)），它们在扫描期打 `NEEDS_GOT`/`NEEDS_PLT` 标志；真正分配发生在后扫描 `postScanRelocations` 的 `addGotEntry`/`addPltEntry`。

2. **`replaceWithDefined` 为何只回填 `versionId`、用 `fetch_and(NEEDS_GOT)` 而非整体拷贝旧 `Symbol`？**
   答：`Defined::overwrite` 只会破坏 `versionId`（对来自 DSO 的符号强制设 `VER_NDX_GLOBAL`）以及本就要覆盖的 `value`/`size`/`section` 等字段；`flags` 与其余位它根本不碰。因此只需暂存 `versionId` 这一个 `uint16_t`（而不是 `memcpy` 整个 `SymbolUnion`），`flags` 则直接对活字段做一次 `fetch_and(NEEDS_GOT)`——原子地「保留 `NEEDS_GOT`、清掉其余」。结果与旧代码 `store(old & NEEDS_GOT)` 一致，但少了一次整结构拷贝。

3. **「链接期常量」与「运行期动态重定位」的区分对输出大小有何影响？**
   答：常量在链接期 patch 进 `.text`，零额外开销；非常量则生成 `.rela.dyn`/`.rela.plt` 条目（每条 `Elf64_Rela` 24 字节）并常伴随 GOT（8B）/PLT（16B）槽位。区分二者让 LLD 只在必要时（PIC + 可抢占 / 绝对引用 / 跨 DSO）才付出动态重定位的体积代价，这正是 `-no-pie` 比 `-pie` 输出更小的根因。

**预期结果**：第 3 步能看到一条针对 `shared_global` 的 `R_X86_64_COPY`，且 `.bss` 段里为其预留了空间。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：copy relocation 为什么对 **只读数据** 要用 `.bss.rel.ro` 而不是普通 `.bss`？
**答案**：见 [ELF/Relocations.cpp:305-310](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L305-L310)。若原符号在只读段，拷贝到普通可写 `.bss` 会丢失「只读」内存保护；放进 `.bss.rel.ro` 可让它落在只读 PT_LOAD 段，既保留保护语义又能在加载时被（具有写权限的）动态链接器写入。

**练习 2**：若把 `replaceWithDefined` 里的 `sym.flags.fetch_and(NEEDS_GOT, ...)` 改成 `sym.flags.store(0, ...)`，会有什么后果？
**答案**：会把一个 copy reloc 别名上仍存在的 `NEEDS_GOT` 标志也清掉，导致该别名在 `postScanRelocations` 中不再分配 GOT 槽（[ELF/Relocations.cpp:1296-1308](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L1296-L1308)），凡是经 GOT 引用该别名的重定位都会拿到错误地址。所以掩码必须保留 `NEEDS_GOT`。

**练习 3**：为什么 `replaceWithDefined` 运行在后扫描阶段（串行），而 `process`/`processAux` 运行在扫描阶段（并行）依然安全？
**答案**：扫描阶段只对 `flags` 做 **原子** 的 `setFlags`/`fetch_or`，不改写符号的种类与地址字段；真正改写槽位内容（`overwrite`、分配 GOT/PLT 索引）都推迟到串行的后扫描，避免多 worker 同时改写同一符号。

## 5. 综合实践

把本讲三节串起来：链接一个 **非 PIE 主程序引用共享库里的全局变量与函数**，跟踪一条重定位从扫描到落地改写符号的完整旅程。

1. 准备共享库与主程序（示例代码同 4.3.4，再补一个导出函数 `int shared_func(void);`）。
2. 链接后用 `readelf -r main` 与 `readelf -S main` 收集证据：
   - 针对变量 `shared_global` 的 `R_X86_64_COPY`（→ 扫描打 `NEEDS_COPY` → 后扫描 `addCopyRelSymbol` → `replaceWithDefined` 指向 `.bss`）；
   - 针对函数 `shared_func` 的 `R_X86_64_JUMP_SLOT` 与 `.got.plt` 槽（→ 扫描打 `NEEDS_PLT` → 后扫描 `addPltEntry`）；
   - `.rela.dyn` 里的 `R_X86_64_RELATIVE`（→ `processAux` 判定非常量、段可写、走 `addRelativeReloc`）。
3. 画一张时序图，把上述每条重定位对应到本讲的源码位置：`scanRelocations` → `scan` → `process`（打标志）→ `processAux`（判定常量/动态/copy）→ `postScanRelocations`（落地：`addGotEntry`/`addPltEntry`/`addCopyRelSymbol`/`replaceWithDefined`）。
4. 最后回到 [ELF/Relocations.cpp:244-255](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/lld/ELF/Relocations.cpp#L244-L255)，用一句话向同伴解释本次 `fetch_and` 改动为何是安全的 NFC 优化。

> 若本地无 clang/ld.lld，至少完成第 3、4 步的纯源码阅读与画图部分——这是理解 LLD 重定位机制最有效的方式。

## 6. 本讲小结

- LLD 把重定位处理分成 **并行扫描**（`scanRelocations`）与 **串行后扫描**（`postScanRelocations`）两阶段：前者只打原子标志位、规划体积；后者才真正分配 GOT/PLT 槽位、生成 copy reloc。拆分的根本原因是 `mmap` 输出需要预知体积。
- 扫描主链是 `scan → process → processAux`：`process` 用 `needsGot`/`needsPlt` 把 `RelExpr` 归类并打标志；`processAux` 再判定每条重定位是链接期常量、动态重定位，还是 copy reloc / canonical PLT 候选。
- `RelExpr` 是把上百种机器重定位归约出的架构无关语义；`isStaticLinkTimeConstant` 基于 PIC 与可抢占性判定常量性，其结果直接决定是否生成 `.rela.dyn` 条目，从而影响输出体积。
- copy relocation（数据→`.bss`）与 canonical PLT（函数→`.plt`）都靠 `replaceWithDefined` 把 `SharedSymbol` **就地** 改写成 `Defined`，得益于所有符号共用等大槽、`overwrite` 原地改字节而指针不变。
- 本次更新：`replaceWithDefined` 不再整体拷贝 `Symbol`，只暂存 `versionId`（因 `Defined::overwrite` 会把它重置为 `VER_NDX_GLOBAL`），并用 `flags.fetch_and(NEEDS_GOT)` 取代「从快照 load+store」——纯 NFC，但更省、更直接。

## 7. 下一步学习建议

- **合成段如何被填充**：本讲只说「后扫描分配了 GOT/PLT 槽」，但槽里的字节是谁、何时写进去的？请进入 u6-l3「合成段：GOT/PLT 与动态表」，读 `GotSection`/`PltSection`/`DynamicSection` 的 `writeTo`。
- **真正施加重定位**：扫描把结果记进了 `InputSection::relocations`，真正 patch 字节发生在 `InputSection::relocateAlloc`/`relocateNonAlloc`（`ELF/InputSection.cpp`），建议顺着这条线读一遍，把「规划」与「执行」闭合。
- **跨架构重定位**：本讲的 `RelExpr` 决策是架构无关的，但 `getRelExpr` 与 `usesOnlyLowPageBits` 等是架构相关的——可对照 u6-l1 的 `ELF/Arch/X86_64.cpp`，再看一个 AArch64 或 RISC-V 的实现，体会 `RelExpr` 抽象的价值。
- **Thunks**：当 `processAux` 之外的远跳转超出寻址范围时，重定位还会和 thunks 交互（u6-l4），那是地址分配迭代收敛的另一条故事线。
