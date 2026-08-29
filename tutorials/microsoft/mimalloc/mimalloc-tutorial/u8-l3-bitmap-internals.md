# 原子位图内部：bitmap.c 的区间查找与 x64 优化

## 1. 本讲目标

在 [u6-l3（arena：1GiB 内存区、64KiB slice 与原子位图分配）](u6-l3-arena-slices-bitmap.md) 中，我们把 arena 的位图当作一个「无锁批发商」来使用；在 [u8-l1（原子抽象与 mi_lock）](u8-l1-atomics-and-lock.md) 中，我们准备好了 `mi_atomic_*` 原语的语言基础。本讲打开这个批发商的机器舱，读完之后你应当能够：

1. 说清楚 `mi_bitmap_t` 的四层数据模型：`bfield → bchunk → chunkmap → bitmap`，以及每层的字节尺寸与缓存行关系。
2. 读懂单字（bfield）级原子操作：为什么「置位用 fetch-or、清位用 fetch-and」比 CAS 循环更快，以及 v3.5 引入的「乐观清位」为什么敢把位先错清再补回来。
3. 跟踪 `mi_bchunk_try_find_and_clear*` 家族的区间查找算法：SWAR 位技巧、跨 bfield 的 `clz/ctz` 配合、失败回滚（restore）路径，以及 AVX2/NEON SIMD 加速分支。
4. 理解 binned bitmap（`mi_bbitmap_t`）的分桶查找如何把「扫 16K 位」降为「扫 64 字节 chunkmap + 1 条 cache line」，并解释 chunk 何时被划入 SMALL/MEDIUM/HUGE 尺寸桶。
5. 对照 arena.c 的真实调用点，说明 `mi_bitmap_t`（平位图）与 `mi_bbitmap_t`（分桶位图）的分工。

## 2. 前置知识

- **位图（bitmap）作为空闲表**：用一个 bit 代表一个固定大小的资源单位。mimalloc 里 1 个 bit 代表 1 个 64 KiB 的 arena slice；bit 为 1 的语义由具体的位图决定——在 `slices_free` 中 **1 = 空闲**，在 `slices_committed` 中 **1 = 已提交**。
- **原子 RMW（read-modify-write）三档**（承接 u5-l1 的三档划分）：普通读写、原子 load/store、原子 RMW（`fetch-or`/`fetch-and`/`CAS`）。位图的核心操作全部落在第三档，但 mimalloc 的策略是**能不用 CAS 就不用**——`lock orq`/`lock andq` 一条指令解决，CAS 失败还要重试循环。
- **位扫描指令**：`tzcnt`（数尾部 0，即找最低位的 1）、`lzcnt`（数头部 0）、`popcnt`（数 1 的个数）、`blsi`（隔离最低位的 1，即 `x & -x`）。它们属于 x86 的 BMI1/BMI2 指令集；AVX2 通常隐含它们。mimalloc 在 [include/mimalloc/bits.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h) 中做了「编译器内建 → MSVC intrinsic → 纯 C」的多级分发（u8-l1 已读）。
- **SWAR（SIMD Within A Register）**：不用 SIMD 寄存器，用普通整数运算的技巧一次性判断 64 位字里「每个字节是否都为 0xFF」。本讲会见到标准 SWAR 模式。
- **保守近似（conservative approximation）**：一个索引结构允许「多报」不允许「漏报」。chunkmap 只承诺「位为 1 ⇒ 对应 chunk **可能**有 1 位」，允许短暂失同步，换取无锁维护。这是本讲反复出现的设计合同。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [src/bitmap.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h) | 位图数据结构与 API 声明 | 四层结构定义、尺寸宏、`mi_bbitmap_t`、`mi_bbitmap_try_find_and_clearN` 分派器 |
| [src/bitmap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c) | 全部实现（约 2000 行） | bfield 原子原语、chunk 区间查找、SIMD 分支、bbitmap 分桶查找 |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | 位图的头号用户 | `mi_arena_try_alloc_at`、abandon 认领、purge 归还 |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 尺寸宏与 `mi_arena_t` | `MI_BCHUNK_BITS`、四张位图字段 |
| [include/mimalloc-stats.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc-stats.h) | `mi_chunkbin_t` 枚举 | 分桶的类型定义 |
| [include/mimalloc/bits.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h) | 位扫描原语 | `mi_ctz/mi_clz/mi_bsf` 的 BMI1 内联汇编 |

注意：bitmap.c 是独立翻译单元（[CMakeLists.txt:L95](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L95)），同时也被 [src/static.c:L27](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/static.c#L27) `#include` 进单文件版本；它的函数不是 `static`，因此链接静态库后测试程序可以直接调用（本讲综合实践正是利用这一点）。

## 4. 核心概念与源码讲解

### 4.1 分层位图数据模型：bfield → bchunk → chunkmap → bitmap/bbitmap

#### 4.1.1 概念说明

一个 1 GiB 的 arena 有 16384 个 slice，需要 16384 个 bit（2 KiB）。如果用一维 bit 数组做「找 8 个连续空闲位」，最坏要线性扫完 2 KiB（32 条 cache line）。mimalloc 的解法是把位图组织成**两级索引**（源码注释里自称 "more or less a btree of depth 2"）：

- **bfield**（bit field）：一个机器字 `size_t`，64 位上有 64 个 bit。
- **bchunk**（bit chunk）：8 个 bfield 拼成 512 bit，**正好 64 字节 = 1 条 cache line**（64 位平台），并按 cache line 对齐。这是刻意的：扫一个 chunk 至多一次内存访问。
- **chunkmap**：每个 chunk 对应 1 个 bit，该 bit 为 1 表示「对应 chunk 里**可能**还有 1 位为 1」。chunkmap 本身也是一个 bchunk（512 bit），于是最多能索引 512 个 chunk。
- **mi_bitmap_t**：chunkmap + N 个 chunk。N 最大 512，对应 512×512 = 262144 bit；乘以 64 KiB/slice 正好是 16 GiB 的 arena 上限。

#### 4.1.2 核心流程

先看数值推导（64 位平台，`MI_SIZE_SHIFT=3`）：

\[ \text{MI\_BCHUNK\_BITS} = 2^{6+3} = 512,\quad \text{MI\_BCHUNK\_SIZE} = 512/8 = 64\,\text{字节（1 cache line）} \]

\[ \text{MI\_BITMAP\_MAX\_BIT\_COUNT} = 512 \times 512 = 262144\,\text{位},\quad 262144 \times 64\,\text{KiB} = 16\,\text{GiB} \]

一次「找空闲区间」的理想路径：

```text
读 chunkmap 的若干字（16 GiB 满配也只有 8 个字）
  → tzcnt 找到第一个可能非空的 chunk          （≤ 2 次访存）
  → 加载该 chunk 的 8 个 bfield（1 条 cache line）
  → 位运算定位区间并原子清零占有               （1 次 RMW）
```

对比无索引的一维扫描：每 GiB 需扫 2 KiB（32 条 cache line）。chunkmap 把最坏扫描量从 \(O(\text{bit 数})\) 压到 \(O(\text{chunk 数}/64 + 1)\)。

chunkmap 是**保守近似**：置位允许晚于 chunk 内置位（「先改 chunk、后设 chunkmap 位」），清位则要经过「测试→清→再测试」三步防竞争（见 4.1.3 第 5 点）。代价是偶尔「有空闲却报没有」，mimalloc 接受这一点以避免全局锁。

#### 4.1.3 源码精读

**1）顶层设计注释**——[src/bitmap.h:L15-L59](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L15-L59)：这段注释是理解全文件的钥匙，明确写出 bfield/bchunk/chunkmap 的分工、「16K bits per GiB」「allocations never span across chunks」等约束。其中 L20 的「We need 16K bits to represent a 1GiB arena」就是容量公式的出处。

**2）bfield 与 bchunk 结构**——[src/bitmap.h:L62-L92](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L62-L92)：`mi_bfield_t` 就是 `size_t`；`mi_bchunk_t` 是 8 个 `_Atomic(mi_bfield_t)` 的数组，用 `mi_decl_bchunk_align` 对齐到 64 字节；`mi_bchunkmap_t` 直接复用 `typedef mi_bchunk_t`。**分配永不跨 chunk**，所以 chunk 数 × 512 × 64 KiB = 32 MiB 就是单次 arena 分配的上限（`MI_ARENA_MAX_CHUNK_OBJ_SLICES`，[include/mimalloc/types.h:L202-L218](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L202-L218)），u4-l3 讲过的 `mi_option_arena_max_object_size` 上限正是由此而来。

**3）mi_bitmap_t**——[src/bitmap.h:L109-L114](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L109-L114)：`chunk_count` 是运行时字段（`_Atomic(size_t)`），`chunks` 数组声明为 1 个元素但实际按 `mi_bitmap_size()` 动态排布——arena.c 把它放在一段预留内存的尾部（[src/arena.c:L1649-L1657](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1649-L1657) 的 `mi_arena_bitmap_init`：指针强转 + 步进 `base`）。顺带一提：[bitmap.h:L105](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L105) 注释写着 `MI_BITMAP_DEFAULT_BIT_COUNT // 2 GiB arena`，但 L101 的默认 chunk 数已是 1（32 MiB），这条注释是旧默认值 128 chunk 时代的遗物——再次印证 u3-l2 的经验：**断言与宏是契约，注释可能滞后**。

**4）尺寸计算**——[src/bitmap.c:L1049-L1074](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1049-L1074)：`mi_bitmap_size(bit_count)` 把 bit 数向上取整到 512 的倍数，返回 `offsetof(mi_bitmap_t, chunks) + chunk_count × 64`；`mi_bitmap_init` 只在 `already_zero=false` 时才清零（arena 的 info 区来自刚保留的零页，省一次 memset），并用 release 序发布 `chunk_count`。

**5）chunkmap 的无锁维护**——[src/bitmap.c:L1024-L1042](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1024-L1042)：

```c
static bool mi_bitmap_chunkmap_try_clear(mi_bitmap_t* bitmap, size_t chunk_idx) {
  if (!mi_bchunk_all_are_clear_relaxed(&bitmap->chunks[chunk_idx])) return false; // ① 测试
  mi_bchunk_clear(&bitmap->chunkmap, chunk_idx, NULL);                            // ② 清
  if (!mi_bchunk_all_are_clear_relaxed(&bitmap->chunks[chunk_idx])) {             // ③ 再测试
    mi_bchunk_set(&bitmap->chunkmap, chunk_idx, NULL);                            // ④ 补回
    return false;
  }
  return true;
}
```

为什么不能直接清？②与③之间若别的线程在 chunk 里置了位而我们不复查，chunkmap 就会**漏报**，违反保守近似合同。置位方向（`mi_bitmap_chunkmap_set`，L1024-L1027）则无条件直接设——多报无害。调用时机同样讲究：`mi_bitmap_setN` 是「先改 chunk、后设 chunkmap」（[src/bitmap.c:L1143](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1143)），保证读者看到 chunkmap 位为 1 时 chunk 的修改必然已可见（acq/release 配合）。

#### 4.1.4 代码实践

**实践目标**：不运行任何东西，纯手工算一遍位图体积，建立数量级直觉。

1. **操作步骤**：
   - 读 [src/bitmap.h:L64-L71](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L64-L71) 的宏定义，写出 64 位平台上 `MI_BFIELD_BITS`、`MI_BCHUNK_FIELDS`、`MI_BCHUNK_SIZE` 的值。
   - 读 [src/bitmap.c:L1049-L1060](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1049-L1060)，计算一个 1024 位的 `mi_bitmap_t` 需要多少字节（提示：`offsetof(chunks)` = 8（chunk_count）+ 56（padding）+ 64（chunkmap） = 128）。
   - 再算 32 位平台（`MI_SIZE_SHIFT=2`，`MI_BCHUNK_BITS=256`，chunk 32 字节）上同样的位图多大。
2. **需要观察的现象**：64 位答案是 128 + 2×64 = **256 字节**；32 位平台 chunk 只有 256 位，1024 位需 4 个 chunk。
3. **预期结果**：一个能覆盖 16 GiB arena 的满配位图共 4096 字节数据 + 64 字节 chunkmap——即每 GiB arena 平均只花约 260 字节元数据。此结果可在综合实践中用 `mi_bitmap_size(1024,NULL)` 打印验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_bchunk_t` 要强制按 cache line 对齐（`mi_decl_bchunk_align`）？
**答案**：一个 chunk 恰好 64 字节。对齐后扫描 chunk 的 8 个 bfield 只触碰 1 条 cache line；若跨两条 line，无竞争的扫描路径也会多付一次潜在的多核一致性流量，且 AVX2 的 `_mm256_load_si256` 两次装载（见 4.3）恰好覆盖一条对齐的 line。

**练习 2**：chunkmap 允许「位已清但 chunk 里其实还有 1」，为什么这是安全的？反过来「位为 1 但 chunk 全空」也允许吗？
**答案**：chunkmap 只用于**加速查找**，位为 0 ⇒ 查找跳过该 chunk ⇒ 最多表现为「暂时找不到空闲位」（分配稍慢或去别的 arena），不破坏正确性。反过来「位为 1 但全空」是典型多报，保守近似明确允许（bitmap.h L36-L40 注释）。两个方向都安全的前提是：位为 0 时绝不能漏掉真实的 1——这正是 `try_clear` 三步复查防的竞争。

**练习 3**：`MI_BITMAP_DEFAULT_CHUNK_COUNT` 为什么从注释里的 128 改成了 1？
**答案**：结构体里的 `chunks[MI_BITMAP_DEFAULT_CHUNK_COUNT]` 只决定**静态声明**的下界；实际 chunk 数在 `mi_bitmap_init` 时按 arena 大小写入 `chunk_count`，存储由 arena 的 info 区动态排布（4.1.3 第 3 点）。默认 1 意味着 `sizeof(mi_bitmap_t)` 只是「最小占位」，真实大小以 `mi_bitmap_size()` 为准。（依据：[bitmap.h:L98-L102](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L98-L102) 的被注释代码与 [bitmap.c:L1049-L1060](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1049-L1060)。）

### 4.2 原子位原语：fetch-or/fetch-and 与「乐观清位」

#### 4.2.1 概念说明

位图的最底层操作是「在一个 `_Atomic(mi_bfield_t)` 字上改若干位」。mimalloc 在 v3.5（commit `6f5bcfd7` "improve atomic bit find and clear"，参考 PR #1346）做了一次关键替换：

- **旧写法**：`load` 期望值 → `cas_weak` 循环直到成功。CAS 失败要重读重试，高竞争下退化为自旋。
- **新写法**：**置位 = 一条 `mi_atomic_or_acq_rel`，清位 = 一条 `mi_atomic_and_acq_rel`**。x86 上直接编译为 `lock orq`/`lock andq`，一条指令、无循环；返回的旧值顺便回答「这些位原来是不是 0（或 1）」。

「占有一个空闲区间」的本质于是变成：**先无锁地找到一段全 1 的位，再用一次原子 AND 把它们清 0；若旧值显示并非全 1（有人抢先），回滚再试**。这就是「乐观清位」（optimistic clearing，commit `af45b7e3`）——注释直言动机："generally an optimistic atomic and/or is more efficient (at least on arm64)"，即 ARM 的 LSE 原子指令下 fetch-and 远快于 CAS 循环，x86 上同样省去重试。

代价是**瞬时的错误状态**：乐观清位可能把本不该清的位先清掉了，再 OR 补回。这期间其他线程可能观察到「位=0」的假象。mimalloc 用 `did_temp_clear_bits` 出参把这件事上报给上层，让 chunkmap 保守地重新置位（见 4.4.3）。

#### 4.2.2 核心流程

单字原语一览（均在 [src/bitmap.c:L87-L229](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L87-L229)）：

| 函数 | 原子指令 | 返回语义 | 用途 |
|---|---|---|---|
| `mi_bfield_atomic_set` | fetch-or | 位从 0→1？ | 置单个位 |
| `mi_bfield_atomic_clear` | fetch-and | 位从 1→0？+ 是否全清 | 清单个位（喂 chunkmap 判断） |
| `mi_bfield_atomic_set_mask` | fetch-or | 掩码位全 0→全 1？+ 原已置位数 | 提交区间（统计 already_set） |
| `mi_bfield_atomic_clear_mask` | fetch-and | 掩码位全 1→全 0？ | 归还区间 |
| `mi_bfield_atomic_try_clear_mask_optimistic` | fetch-and（失败补 fetch-or） | 掩码位是否原子地全 1→0 | **分配的占有步骤** |
| `mi_bfield_atomic_clear_once_set` | load + fetch-and | 无返回 | 「等到位为 1 再清」，忙等 |

「找 8 位并占有」的完整时序（单字内情形）：

```text
b = load_relaxed(field)                 # 快照
has_set8 = SWAR 判断哪个字节 == 0xFF     # 找候选
bitidx = tzcnt(has_set8)                # 最低候选字节
old = atomic_and(field, ~mask)          # 乐观清位：一次 RMW
if (old & mask) == mask: 成功，区间归我
else: 原来就不全 1 → atomic_or(old & mask) 补回误清的位 → 重试(≤4 次)
```

#### 4.2.3 源码精读

**1）fetch-or 置位 + 统计**——[src/bitmap.c:L134-L139](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L134-L139)：

```c
static inline bool mi_bfield_atomic_set_mask(_Atomic(mi_bfield_t)* b, mi_bfield_t mask, size_t* already_set) {
  const mi_bfield_t old = mi_atomic_or_acq_rel(b, mask);   // 一条 lock orq
  if (already_set != NULL) { *already_set = mi_bfield_popcount(old & mask); }
  return ((old & mask) == 0);                              // 全 0→全 1 才算"干净转移"
}
```

`already_set` 出参服务 arena 的 commit 统计：在半提交区域上再 commit 时，只有新提交的部分才该计入 `committed` 统计（调用点 [src/arena.c:L319-L326](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L319-L326) 的 `mi_bitmap_setN(..., &already_committed_count)` 技巧：先 set 再 clear，只为数出原来已置位的个数）。

**2）乐观清位核心**——[src/bitmap.c:L163-L182](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L163-L182)：

```c
static inline bool mi_bfield_atomic_try_clear_mask_optimistic(_Atomic(mi_bfield_t)* b, mi_bfield_t mask,
                                                              mi_bfield_t* previous, bool* did_temp_clear_bits) {
  mi_bfield_t old = mi_atomic_and_acq_rel(b, ~mask);      // 无条件先清
  if (previous != NULL) { *previous = old; }
  if mi_likely((old & mask) == mask) { return true; }     // 原来全是 1：真占有
  else {
    if ((old & mask) != 0) {                              // 有些位原来就是 0，被我顺手清了（无效果）
      mi_atomic_or_acq_rel(b, old & mask);                // 补回"原本为 1"的那部分
      if (did_temp_clear_bits != NULL) { *did_temp_clear_bits = true; }
    }
    return false;
  }
}
```

三个细节值得咀嚼：(a) `old & mask` 为非 0 非 mask 时，其中为 1 的位被我错误清掉，必须 OR 回 `old & mask`；(b) 这些位**曾短暂为 0**，并发读者可能据此做错判断，所以 `did_temp_clear_bits` 必须上报；(c) 注意 `old & ~mask` 为 0 时上层还能知道「现在整个字全空」（`mi_bfield_atomic_clear` 的 `all_clear` 出参），这是 chunkmap 维护的输入。

**3）先查后试的保守版本**——[src/bitmap.c:L207-L216](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L207-L216)：`mi_bfield_atomic_try_clear_mask` 先 `load_relaxed` 检查掩码位是否全 1，大概率命中才走乐观版本，避免无谓地「清了又补」。用于失败成本高的多字场景。

**4）忙等清位**——[src/bitmap.c:L112-L129](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L112-L129)：`mi_bfield_atomic_clear_once_set` 语义是「这位**将来一定会被置 1**，我等它置 1 后清掉」。它用 `_mi_prim_thread_yield` 忙等并把等待次数计入 `pages_unabandon_busy_wait` 统计。这是 u6-l4 讲过的 abandon 竞争的仲裁点：跨线程 free 想把页从 `pages_abandoned` 位图摘掉，而分配方正握着它尝试认领，free 方只能等认领方先把位置 1。

**5）底层位扫描来自 bits.h**——[include/mimalloc/bits.h:L217-L239](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L217-L239) 的 `mi_ctz` 在 GCC+x64+BMI1 下直接内联 `tzcnt` 汇编；[L275-L287](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L275-L287) 的 `mi_bsf` 更进一步用 `=@ccc`(carry 条件输出) 让「x==0」的判定免费附带。bitmap.c 的 L26-L54 把它们薄封装成 `mi_bfield_*`（ctz/clz/popcount/find_least_bit/find_highest_bit）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 v3.5 前后原子写法的差异。

1. **操作步骤**：在仓库根目录执行
   ```bash
   git show 6f5bcfd7 -- src/bitmap.c | head -80
   git show af45b7e3 -- src/bitmap.c | head -90
   ```
2. **需要观察的现象**：第一个提交把 `mi_bfield_atomic_set_mask`/`clear_mask` 里的 `while (!mi_atomic_cas_weak_acq_rel(...)) {}` 循环整体替换成单条 `mi_atomic_or_acq_rel`/`mi_atomic_and_acq_rel`；第二个提交把「load + 检查 + CAS 循环」的 `try_clear_mask_of` 改造成无条件 AND + 失败补 OR 的 `_optimistic` 版本。
3. **预期结果**：能说出每个被删掉的循环在 x86 上对应 `lock cmpxchg` 重试，而替换后是一条 `lock orq`/`lock andq`。若想看真实指令，可在构建目录对 bitmap.c 所在目标执行 `objdump -d libmimalloc.a | grep -A3 "lock"`（待本地验证，具体输出取决于编译器版本与 -O 级别）。

#### 4.2.5 小练习与答案

**练习 1**：`mi_bfield_atomic_set_mask` 返回 false 是否意味着置位失败？
**答案**：不是。fetch-or **总会**把掩码位置 1；返回 false 只说明「这些位里原本已有 1」，即不是一次「全 0→全 1 的干净转移」。arena 的 `mi_bbitmap_setN` 归还路径正靠这个返回值检测 double free（[src/arena.c:L1474-L1478](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1474-L1478)，返回 false 即报 `EAGAIN` "trying to free an already freed arena block"）。

**练习 2**：乐观清位为什么必须把「补回」也做成原子 OR，而不能直接把字写回快照值？
**答案**：从 AND 返回到补回之间，其他线程可能又改了同一字的其他位。直接写快照会用旧值覆盖别人的修改（丢失更新）。原子 OR 只动 `old & mask` 这些位，与并发修改正交。

**练习 3**：既然单 bit 清位 `mi_bfield_atomic_try_clear_optimistic`（[bitmap.c:L188-L192](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L188-L192)）也走乐观路径，为什么它的注释说 "single bit never clears temporarily"？
**答案**：单 bit 掩码只有 1 位。若该位原为 1，AND 后干净转移成功；若原为 0，AND 前后值不变，`old & mask == 0`，无需补回任何位。所以单 bit 永不出现「误清了原本为 1 的位」，`did_temp_clear_bits` 恒为 false。

### 4.3 区间查找与认领：try_find_and_clear 家族与 SIMD 加速

#### 4.3.1 概念说明

这一层解决「**在一个 chunk（512 bit）内**找一段连续 n 个 1 并原子清零占有」。按 n 的大小分四个特化实现，越常用的越特化：

| n | 函数 | 对应场景（结合 u3-l3 的页尺寸） |
|---|---|---|
| 1 | `mi_bchunk_try_find_and_clear` | 小页（64 KiB，1 slice） |
| 8 | `mi_bchunk_try_find_and_clear8` | 中页（512 KiB，8 slice） |
| ≤64 | `mi_bchunk_try_find_and_clearNX` | 跨字但不跨 chunk 的一般区间 |
| ≤512 | `mi_bchunk_try_find_and_clearNC` | 跨多个字、至多一整个 chunk（大页 4 MiB=64 slice 等） |

n=1 与 n=8 是分配的绝对大头，所以它们享有最重的优化：n=1 走「`b & -b` 隔离最低位」三行快路径；n=8 走 SWAR/SIMD 的「找 0xFF 字节」。**注意 n=8 找的是按 8 对齐的字节**（`bitidx % 8 == 0`），这与 arena 的 8-slice 对齐分配约定吻合。

#### 4.3.2 核心流程

**n=1 的快路径**（[bitmap.c:L594-L612](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L594-L612)）：

```text
b = load(field)
mask = b & (~b + 1)          # blsi：隔离最低位的 1
old = atomic_and(field, ~mask)
if old & mask:  成功，tzcnt(mask) 即位下标
else:          有人抢先，最多重试 4 次后放弃（换下一个 chunk）
```

**n=8 的候选查找（SWAR 版）**（[bitmap.c:L713-L739](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L713-L739)）：不逐位扫，而是一次运算判断 64 位字里**哪个字节等于 0xFF**：

\[ \text{has\_set8} = \frac{\big((\lnot b - \texttt{0x0101..01}) \wedge (b \wedge \texttt{0x8080..80})\big)}{128} \]

原理：`~b - 0x01..` 让「b 中 < 0x7F 的字节」在最高位产生借位传播……实际判断是两条经典分支的交集——`\(~b - LO\)` 的最高位为 1 当且仅当该字节 `b_byte ≤ 0x7F` 或恰为 `0xFF`（借位吞掉），再与「该字节 ≥ 0x80」相与，两个条件同时成立只剩 `0xFF`。得到 has_set8 后一次 `tzcnt` 直接命中最低的满字节。

**跨字区间（NX）的推进技巧**（[bitmap.c:L793-L849](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L793-L849)）：在字内从最低 1 位 `idx` 起试探 n 位是否全 1；若 `b & bmask != bmask`，不用逐位前移，而是一步跳过**当前这段连续 1**：

```text
b = b & (b + (1 << idx))    # 借位吃掉从 idx 开始的连续 1，落在新的一段上
```

源码注释给出了 n≥4、idx=2 的算例（L821-L826）。跨字边界时用 `mi_bfield_clz(~b)`（尾部连续 1 的个数）与下一字的 `mi_ctz(~b')`（头部连续 1 的个数）拼出跨越区间，再交给 `mi_bchunk_try_clearNX` 两字版本占有。

#### 4.3.3 源码精读

**1）n=1 的占有**——[src/bitmap.c:L594-L612](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L594-L612)：`mask = (b & (~b+1))` 旁边有注释 "≈ blsi 但避免编译器警告"；重试上限 `tries <= 4` 用于限制竞争下的自旋浪费——找不到就返回 false 让上层换 chunk。

**2）AVX2 加速的 chunk 扫描**——[src/bitmap.c:L636-L674](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L636-L674)：64 位平台上一个 chunk 就是 64 字节 = 两条 256 位向量。代码一次性装载 `vec1/vec2`，`_mm256_cmpeq_epi64(., 0)` 找出**全零**的字，`movemask` 反转后得到「哪些字非零」的 64 位掩码，再一次 `mi_ctz` 直接定位最低非零字——把「扫 8 个字」压缩成几条向量指令。注意这段属于 `#if MI_OPT_SIMD && defined(__AVX2__)` 分支，而 **MI_OPT_SIMD 默认为 0**（[bitmap.c:L18-L20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L18-L20)），默认构建走 L701-L704 的标量循环——SIMD 是编译期可选实验路径。

**3）编译器屏障逸事**——[src/bitmap.c:L667-L674](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L667-L674)：SIMD 重试循环里有 `__asm __volatile ("" : : "g"(chunk) : "memory")`，注释指向 issue #1206——老版本 GCC 即使有原子 acquire 也不重载向量寄存器，导致读到陈旧值死循环。一条空内联汇编强制重新加载。这是「原子语义之外还要提防编译器缓存」的真实案例，与 u8-l1 的编译器屏障话题呼应。

**4）NEON 版本**——[src/bitmap.c:L675-L699](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L675-L699)：ARM64 上用 `vceqzq_u64` + 一串 unzip/narrow 把 8 个字的「是否非零」压缩进 1 个 64 位掩码，思路与 AVX2 完全对称。同目录还有 n=8 的 AVX2 版（[L746-L774](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L746-L774)，用 `_mm256_cmpeq_epi8` 找 0xFF 字节）。

**5）多字占有与回滚**——[src/bitmap.c:L504-L565](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L504-L565)：`mi_bchunk_try_clearNC` 是「复杂的那一个」（源码注释原话）——首字、中间整字、尾字分别原子清，任何一步失败就 `goto restore` 把已清的字 OR 回去。回滚按「中间字整字 setX、首字只 set 掩码」精确恢复。跨 chunk 的更大区间在 [L1919-L1943](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1919-L1943) 的 `mi_bchunk_try_clearN_` 里做同样的事，且**回滚时保守地设 chunkmap 位**（L1940），因为整 chunk 被清空又补回的过程必然产生瞬时全 0 状态。

**6）mi_bitmap_find：平位图的遍历框架**——[src/bitmap.c:L1306-L1328](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1306-L1328)：先扫 chunkmap 字，再经 `mi_bfield_cycle_iterate` 宏（[L1266-L1294](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1266-L1294)）按 `tseq % cycle` 的起点**环状**遍历候选 chunk——不同线程从不同位置开始找，天然错开竞争点（注释："space out threads"）。找到候选后回调 `on_find` 访问器做实际占有。

**7）认领与放弃**——[src/bitmap.c:L1340-L1380](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1340-L1380)：`mi_bitmap_try_find_and_claim` 是 abandon 认领的入口（u6-l4 的 reclaima 路径）：找到 1 位→清 0→调用 `claim` 回调（arena 的 `mi_arena_try_claim_abandoned`）做页级原子接管；**接管失败则把位设回 1**（L1357-L1362），位图状态完整回退。这是「查找与占有合一」在平位图上的形态。

#### 4.3.4 代码实践

**实践目标**：手工模拟 SWAR 字节检测，确认自己真的懂了公式。

1. **操作步骤**：取 `b = 0x00FF_FFFF_0000_0000`（字节 5、6、7 为 0xFF）。按 [bitmap.c:L720-L723](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L720-L723) 的公式手算 `has_set8`（`MI_BFIELD_LO_BIT8=0x0101..01`，`MI_BFIELD_HI_BIT8=0x8080..80`，最后右移 7 位）。
2. **需要观察的现象**：`has_set8` 的第 5 位应为 1（字节 5 是最低的 0xFF 字节），其余 0xFF 字节对应位也应为 1，非 0xFF 字节对应位为 0；`tzcnt(has_set8) = 40`，即区间起点 bit 40。
3. **预期结果**：写下 8 字节逐字节的中间值。若想自动验证，可把公式抄进任意 C 程序打印（示例代码，仓库中不存在）：
   ```c
   uint64_t b = 0x00FFFFFF00000000ULL;
   uint64_t has_set8 = ((~b - 0x0101010101010101ULL) & (b & 0x8080808080808080ULL)) >> 7;
   printf("has_set8=%#lx, first byte=%zu\n", has_set8, (size_t)__builtin_ctzll(has_set8)/8);
   ```
   预期输出 `first byte=5`。

#### 4.3.5 小练习与答案

**练习 1**：`mi_bchunk_try_find_and_clear_at` 为什么限制 `tries <= 4` 就放弃？
**答案**：该函数只在**一个字**上尝试。持续失败意味着这个字竞争激烈（或快照总是过期），继续自旋的期望收益低于换一个 chunk。放弃后外层会尝试同 chunk 的其他字或按 chunkmap 换 chunk，最终可能去别的 arena——这是 mimalloc 一贯的「让步而不是硬等」风格（对比 4.2.3 第 4 点那种不得不等的场景）。

**练习 2**：`mi_bchunk_try_find_and_clearNX` 的跨字拼接为什么用 `clz(~b)` 而不是 `clz(b)`？
**答案**：`~b` 把「字尾部的连续 1」变成「~b 尾部的连续 0」，`clz(~b)` 数出的是 b 尾部连续 1 的个数 `post`；下一字用 `ctz(~b')` 数出头部连续 1 的个数 `pre`。`post + pre >= n` 即跨越边界的连续 1 足够长（[bitmap.c:L830-L846](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L830-L846)）。直接对 b 用 clz 数出的是「头部连续 0」，与需要的语义相反。

**练习 3**：既然 AVX2 版本更快，为什么默认不启用（`MI_OPT_SIMD` 默认 0）？
**答案**：启用它需要编译期就带 `-mavx2`，这会改变整个库的目标基线，影响在老 CPU 上的可运行性；而标量路径已有 BMI 的 tzcnt/blsi 加持，瓶颈并不显著。当前它作为实验/可选优化保留（[bitmap.c:L18-L20](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L18-L20)），加上 issue #1206 一类编译器兼容坑（4.3.3 第 3 点），默认关闭是稳妥取舍。

### 4.4 bbitmap 分桶查找与 arena 集成、v3.5 原子优化动机

#### 4.4.1 概念说明

4.1 的 chunkmap 解决了「别扫全部 chunk」，但还有一个碎片化问题：若 1-slice 的小请求不断蚕食 chunk，大请求（比如 64 slice）将找不到**连续**的 512 位——尽管总空闲量充足。`mi_bbitmap_t`（binned bitmap）在 chunkmap 之上再加一层**尺寸桶**：每个 chunk 被标注「它 currently 服务哪种大小的分配」（`mi_chunkbin_t`），查找时**只搜目标桶与未分桶的 chunk**，小分配永远不去动留给大分配的 chunk。这就是 u6-l3 已从外部看到的「chunk 按 SMALL/MEDIUM/LARGE/HUGE 尺寸箱标记，仅搜目标箱与全空箱以抑制碎片」的内部实现。

平位图 `mi_bitmap_t` 与分桶位图 `mi_bbitmap_t` 在 arena.c 中的分工（字段声明见 [include/mimalloc/types.h:L749-L752](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L749-L752)）：

| 位图 | 类型 | 语义（1 = ） | 需要查找吗 |
|---|---|---|---|
| `slices_free` | **bbitmap** | slice 空闲 | **是**——分配必须「找连续 n 个 1 并清零」，故用分桶版 |
| `slices_committed` | bitmap | slice 已提交 | 否——只按已知下标 set/clear/查询 |
| `slices_dirty` | bitmap | slice 可能非零 | 否——同上（用于 initially_zero 判定） |
| `slices_purge` | bitmap | slice 待 purge | 否——purge 时整段遍历（`forall_setc_rangesn`） |
| `pages_abandoned[bin]` | bitmap | 该 slice 起始一个被遗弃页 | **是**——但只找单 bit（`try_find_and_claim`），无需分桶 |

#### 4.4.2 核心流程

`mi_bbitmap_try_find_and_clear_generic`（[bitmap.c:L1801-L1884](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1801-L1884)）的主循环骨架：

```text
按 tseq 环状遍历 chunkmap 的字（cmap_cycle ≤ 已访问最高 chunk）
  读 chunkmap 字 cmap_entry（非 0 才继续）
  cmap_bins[NONE] = cmap_entry 减去所有已分桶位     # 未分桶 = 候选"处女" chunk
  for ibin in {SMALL, OTHER, ..., 目标桶 bbin, ..., NONE}:   # 关键顺序
      if ibin == bbin: 之后直接跳 NONE              # 只搜"恰好同桶"与"未分桶"
      环状遍历 cmap_bins[ibin] 的每个 chunk:
          on_find(chunk, n):  # 即 4.3 的 try_find_and_clear*
              成功: 若 cidx==0 且 ibin==NONE → 把该 chunk 划入 bbin 桶; 返回
              失败: 维护 chunkmap（try_clear 或保守 set）
```

桶的划分规则 `mi_chunkbin_of`（[bitmap.h:L249-L257](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L249-L257)）：n=1→SMALL，n=8→MEDIUM（若启用大页则 n=64→LARGE），n>512→HUGE，其余→OTHER。桶状态记录在 `chunkmap_bins[MI_CBIN_COUNT-1]` 这组**附加 chunkmap**（每桶一张 512 位）上，并把每桶 chunk 数计入统计 `chunk_bins`（可用 `mi_debug_show_arenas()` 观察）。

v3.5 优化的动机链条（学习目标 3 的正面回答）：arena 分配是**每一次慢路径页分配的必经点**（u4-l2 的 `mi_page_fresh`→arena），其热点恰恰是 4.2 的单字原子操作。把占有步骤从「CAS 重试循环」换成「一条 fetch-and + 失败补 fetch-or」，等于把无竞争情形的指令数砍半、把有竞争情形从循环变成常数条指令；ARM64（LSE）与 x86 同时受益。两个提交（`6f5bcfd7`、`af45b7e3`）合起来完成了这次替换，代价是需要全链路传播 `did_temp_clear_bits` 以维持 chunkmap 的保守性。

#### 4.4.3 源码精读

**1）arena 分配的占有瞬间**——[src/arena.c:L240-L246](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L240-L246)：`mi_arena_try_alloc_at` 第一句实质操作就是 `mi_bbitmap_try_find_and_clearN(arena->slices_free, tseq, slice_count, &slice_index)`——4.2/4.3/4.4 三层在此汇合：分桶选 chunk（本节）→ chunk 内找区间（4.3）→ 单字乐观清位占有（4.2）。失败返回 NULL 去下一个 arena。后续 L253-L334 用**平位图**记账 dirty/committed 状态，印证上表的分工。

**2）入口分派器**——[src/bitmap.h:L333-L341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L333-L341)：`mi_bbitmap_try_find_and_clearN` 按 n 选特化实现（1→`_clear`、8→`_clear8`、≤64→`NX`、≤512→`NC`、更大→`N_`），是典型的「按尺寸分派」微优化，与 u3-l3 `mi_bin` 的分档思想同源。

**3）桶分配与晋升**——[src/bitmap.c:L1638-L1650](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1638-L1650)：`mi_bbitmap_set_chunk_bin` 在目标桶置位、其余桶清位并同步统计。触发点在 [L1856-L1863](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1856-L1863)：`cidx==0 && ibin==MI_CBIN_NONE`——即「第一次从一个处女 chunk 的第 0 位分配」时，该 chunk 从此归属这种尺寸。当 chunk 重新全空闲时晋升回 NONE 桶（[L1670-L1680](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1670-L1680) 的 `mi_bbitmap_chunkmap_set` 在 `check_all_set` 时调 `set_chunk_bin(MI_CBIN_NONE)`）。

**4）`chunk_max_accessed` 高水位**——[src/bitmap.c:L1662-L1667](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1662-L1667) 与 [L1804-L1807](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1804-L1807)：bbitmap 记录「历史访问过的最高 chunk」，查找的环状循环以它为上界——优先在已用区域里复用（提升局部性、少碰新页），这与 u4-l2 `mi_find_page`「优先复用更满的页」是同一哲学在内存层的投影。

**5）失败路径的 chunkmap 保守维护**——[src/bitmap.c:L1865-L1877](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1865-L1877)：`on_find` 失败时二选一——若 `did_temp_clear_bits`（4.2 的瞬时误清发生过），直接保守 `chunkmap_set`；否则尝试 `chunkmap_try_clear`（三步复查）。这是「乐观清位」向上层泄漏的信息在 bbitmap 层的最终消费点。

**6）purge 的成段遍历**——[src/arena.c:L2321-L2329](https://github.com/microsoft/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2321-L2329) 与 [L2383](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2383)：purge 到期时用 `mi_bbitmap_try_clearNC` 把一段空闲位「临时占用」（防止并发分配插手）、执行 `_mi_os_purge`，再 `mi_bbitmap_setN` 放回。而 `_mi_bitmap_forall_setc_rangesn`（[bitmap.c:L1517-L1575](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1517-L1575)）以 `rngslices` 为粒度成段交换出置位区间——u6-l2 讲过 purge 粒度在 THP 下取 2 MiB，正对应这里「只按 32 slice 的倍数成段归还，避免撕碎巨页」。

#### 4.4.4 代码实践

**实践目标**：用公开 API 观察尺寸桶的真实分布。

1. **操作步骤**：写一个小程序（示例代码，仓库中不存在）：先 `mi_malloc(1)` 触发初始化；然后循环分配 32 MiB（如 `for(i=0;i<32;i++) mi_malloc(1<<20)` 再混入小分配）；最后调用 [mi_debug_show_arenas()](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L332)。
2. **需要观察的现象**：输出的 chunk 展示中包含按 `mi_bbitmap_debug_get_bin`（[bitmap.c:L1652-L1659](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1652-L1659)）标注的桶信息（arena.c 的调试打印在 [L2064](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2064) 调用它）；也可用 `MIMALLOC_VERBOSE=1` 辅助确认 arena 的创建。
3. **预期结果**：能看到小分配与大分配落在不同 chunk 集合（不同桶标记），而不是交错撕碎同一批 chunk。具体输出格式**待本地验证**（依赖 `MI_STAT` 级别与构建类型，建议用 debug 构建）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `pages_abandoned` 用平位图 + 单 bit 认领（`mi_bitmap_try_find_and_claim`），而不做成 bbitmap？
**答案**：认领页永远一次拿 1 个 bit（一个页的起始 slice），不存在「找连续 n 位」的需求，bbitmap 的分桶机制无用武之地；而它需要的「找到→清零→页级接管→失败回滚置位」恰好是 `try_find_and_claim` + claim 回调的形状（4.3.3 第 7 点，调用点 [arena.c:L747-L754](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L747-L754)）。此外它按页尺寸 bin 分成了 `MI_ARENA_BIN_COUNT` 张平位图，等价于「按对象尺寸分桶」的另一种更粗的实现。

**练习 2**：一个 chunk 一旦划入 MEDIUM 桶，小分配（SMALL）还会碰它吗？什么时候例外？
**答案**：不会，除非它回到 NONE 桶。查找顺序（4.4.2）从 SMALL 出发但会跳过所有已分桶位，只有 `ibin == bbin` 的同桶 chunk 与 NONE 的处女 chunk 会被搜索；chunk 重新全空时经 `mi_bbitmap_chunkmap_set(check_all_set=true)` 晋升回 NONE（4.4.3 第 3 点），此后任何尺寸都能用。

**练习 3**：`mi_bbitmap_try_find_and_clearN_`（[bitmap.c:L1950-L1997](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1950-L1997)）为什么「只考虑从 chunk 起点开始」且失败后 `count=0` 重试时还要跳过一个 chunk？
**答案**：巨对象（n>512，跨多 chunk）本身稀少，为降低碎片与实现复杂度，直接要求从整 chunk 边界起占（注释 "for fragmentation and efficiency"）。失败说明发生竞争，若原地重试可能与对手再次碰撞，跳过当前 chunk 保证前进性（"we still skip the first chunk to guarantee progress"）。

## 5. 综合实践

把 4.1–4.4 串成一个可运行的实验：**构造一个 1024 位的位图，反复执行「找 8 个连续空闲位并占有」，打印每步状态，并验证尾部不足 8 位时返回 false**。因为 `mi_bitmap_t` 上只有单 bit 认领，「找 8 位」必须用 `mi_bbitmap_t`（这正是 4.4.1 分工表的结论），所以实验分 A/B 两部分。

**准备工作**：按 [u1-l2](u1-l2-build-and-run.md) 完成 release 构建（`mkdir -p out/release && cd out/release && cmake ../.. && make`，产物 `out/release/libmimalloc.a`）。

**第 1 步：写实验程序** `bmp-play.c`（放在仓库根目录；**示例代码**，仓库中不存在）：

```c
// bmp-play.c —— 手动驱动 mimalloc 内部位图（示例代码）
#include <stdio.h>
#include <stdint.h>
#include "mimalloc.h"
#include "mimalloc/internal.h"   // _mi_subproc()
#include "bitmap.h"              // src/bitmap.h（需 -I src）

static _Alignas(64) uint8_t bmp_buf[512];    // 平位图缓冲
static _Alignas(64) uint8_t bbmp_buf[1024];  // 分桶位图缓冲

static void dump_words(const char* tag, mi_bchunk_t* chunks, size_t nchunks) {
  printf("%s", tag);
  for (size_t c = 0; c < nchunks; c++)
    for (int f = 0; f < MI_BCHUNK_FIELDS; f++)
      printf(" %016llx", (unsigned long long)mi_atomic_load_relaxed(&chunks[c].bfields[f]));
  printf("\n");
}

int main(void) {
  mi_free(mi_malloc(8));                       // 触发初始化，确保 _mi_subproc() 可用

  /* ---- Part A: 平位图 mi_bitmap_t 的区间置位/清位与边界 ---- */
  size_t cc;
  const size_t sz = mi_bitmap_size(1024, &cc);
  printf("bitmap: size=%zu bytes, chunks=%zu (expect 256/2)\n", sz, cc);
  mi_bitmap_t* bm = (mi_bitmap_t*)bmp_buf;
  mi_bitmap_init(bm, 1024, /*already_zero=*/false);
  mi_bitmap_unsafe_setN(bm, 0, 1024);          // 全部标记为"空闲"（set=free，同 slices_free 约定）
  printf("popcount after fill = %zu (expect 1024)\n", mi_bitmap_popcount(bm));
  printf("is_setN(0,8)   = %d (expect 1)\n",  mi_bitmap_is_setN(bm, 0, 8));
  printf("is_setN(1016,8)= %d (expect 1)\n",  mi_bitmap_is_setN(bm, 1016, 8));
  mi_bitmap_clearN(bm, 1020, 4);               // 挖掉尾部 4 位
  printf("is_setN(1020,8)= %d (尾部被钳制到 4 位, expect 0)\n", mi_bitmap_is_setN(bm, 1020, 8));
  printf("setN(0,8) again returns %d (已置位→非干净转移, expect 0)\n",
         (int)mi_bitmap_setN(bm, 0, 8, NULL));

  /* ---- Part B: 分桶位图 mi_bbitmap_t 的"找 8 位并占有" ---- */
  mi_bbitmap_t* bb = (mi_bbitmap_t*)bbmp_buf;
  const size_t bsz = mi_bbitmap_init(_mi_subproc(), bb, 1024, false);
  printf("bbitmap: size=%zu bytes\n", bsz);
  mi_bbitmap_unsafe_setN(bb, 0, 1024);
  size_t idx = 0, round = 0;
  while (mi_bbitmap_try_find_and_clear8(bb, /*tseq=*/0, &idx)) {
    round++;
    if (round <= 3 || round >= 127)
      printf("claim #%3zu: slice_index=%4zu, popcount=%zu\n",
             round, idx, mi_bitmap_popcount((mi_bitmap_t*)NULL) /* 占位，见下 */);
  }
  printf("total claims = %zu (expect 128), exhausted->false ok\n", round);
  mi_bbitmap_setN(bb, 1018, 6);                // 只在尾部放 6 个空闲位
  printf("tail-only 6 free bits -> find8 returns %d (expect 0)\n",
         mi_bbitmap_try_find_and_clear8(bb, 0, &idx));
  dump_words("bbitmap chunk words:", bb->chunks, 2);
  return 0;
}
```

上面 `popcount` 一行的占位写法是示意；bbitmap 的计数应改为对 `bb->chunks` 手工 popcount，或直接删掉该列（示例代码，允许读者自行修正——这本身就是练习的一部分）。

**第 2 步：编译链接**（利用 bitmap.c 的符号在静态库中可见，见第 3 节）：

```bash
gcc -O2 -I include -I src -o bmp-play bmp-play.c out/release/libmimalloc.a -lpthread
./bmp-play
```

**第 3 步：需要观察的现象与预期结果（待本地验证）**：

1. Part A：`size=256, chunks=2`；`is_setN(1020,8)` 在挖掉尾部 4 位后返回 0——注意它不是「越界报错」而是被 `maxbits` 钳制成查 4 位（4.1.3 第 4 点的 paranoia 分支，[bitmap.c:L1220-L1227](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1220-L1227) 同款逻辑）；重复 `setN` 返回 0 印证练习 4.2-1 的「非干净转移」。
2. Part B：128 次 claim 依次得到 `slice_index = 0, 8, 16, ...`（tseq=0 时环状遍历从 0 起点、处女 chunk 0 先被划入 MEDIUM 桶，依据 4.4.2 的推导）；第 129 次调用返回 false；释放尾部 6 位后再找 8 位仍返回 false——**因为 find8 找的是「对齐的满字节 0xFF」，尾部 6 位凑不满一个字节**（4.3.2 的 SWAR 语义），这正是本实践要验证的边界行为。
3. 最后的 chunk 字 dump 应只剩最高字低位有零星置位。

**第 4 步：扩展验证（选做）**：把 Part B 的 `try_find_and_clear8` 换成 `mi_bbitmap_try_find_and_clearN(bb, 0, 64, &idx)`（一次占一个整字，对应 LARGE 桶），观察 claim 序列变为 0, 64, 128...，并解释为什么处女 chunk 这次被划入 `mi_chunkbin_of(64)` 对应的桶（提示：[bitmap.h:L249-L257](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.h#L249-L257)，注意 `MI_ENABLE_LARGE_PAGES` 是否开启会影响 `n=64` 落在 LARGE 还是 OTHER）。

## 6. 本讲小结

- 位图是**四层结构**：64 位 bfield → 512 位/64 字节 bchunk（恰好一条 cache line）→ 512 位 chunkmap（保守近似索引，最多覆盖 16 GiB）→ bitmap/bbitmap 本体；每 GiB arena 平均只花约 260 字节元数据。
- **v3.5 原子优化**：置位用一条 fetch-or、清位用一条 fetch-and 取代 CAS 重试循环；「占有」改为乐观清位——先无条件 AND、看旧值不干净再 OR 补回，瞬时错误状态用 `did_temp_clear_bits` 向上层传播以维持 chunkmap 保守性。
- **区间查找按尺寸特化**：n=1 走 `b & -b`（blsi）三行快路径；n=8 走 SWAR「找 0xFF 字节」；跨字用 `clz(~b)/ctz(~b')` 拼接与 `b & (b + (1<<idx))` 跳段；多字失败有精确回滚。AVX2/NEON 分支存在但 `MI_OPT_SIMD` 默认关闭。
- **bbitmap 分桶**用一组附加 chunkmap 把 chunk 标成 SMALL/MEDIUM/LARGE/HUGE/NONE 桶，查找只搜同桶与处女 chunk，用轻微空间换碎片抑制；`chunk_max_accessed` 高水位让查找优先复用已用区域。
- **平位图与分桶位图分工**：只有 `slices_free` 需要「找连续 n 位」而用 bbitmap；committed/dirty/purge 只按已知下标操作用平位图；`pages_abandoned` 只找单 bit，用平位图 + claim 回调。
- 查找全程**无锁、无 ABA**：因为「查找只是建议，占有靠一次原子 RMW」——竞争的输家得到的只是 false，而不是错误状态。

## 7. 下一步学习建议

本讲补完了 [u8-l1](u8-l1-atomics-and-lock.md)、[u8-l2](u8-l2-freelist-sharding-design.md) 之后并发基础的最后一块。建议接下来：

1. 进入单元九的 [u9-l1（安全模式）](u9-l1-secure-mode.md)，看 secure 模式如何把随机 key 引入 free list，与位图层的 `clear_once_set` 忙等形成对照——两种「等待」的代价权衡。
2. 重读 [u6-l4（abandon 与 reclaim）](u6-l4-abandon-reclaim.md) 中 reclaima/reclaimf 两条路径，现在你能精确指出它们各自落在 `mi_bitmap_try_find_and_claim`（[bitmap.c:L1375-L1380](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1375-L1380)）与 `mi_bitmap_clear_once_set`（[bitmap.c:L1426-L1432](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L1426-L1432)）的哪一行。
3. 有余力的读者可以对比 Linux 内核的 buddy allocator 位图（`__find_next_zero_bit` 家族）与本讲的 chunkmap 方案：两者都用「字级加速 + 保守索引」，但内核无原子分桶需求，可以体会 mimalloc「查找与占有合一」设计的独特性。
