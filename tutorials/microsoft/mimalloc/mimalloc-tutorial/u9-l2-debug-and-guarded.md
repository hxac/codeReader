# u9-l2 debug 模式、padding 校验与 guarded 采样

## 1. 本讲目标

上一讲（u9-l1）我们分析了 MI_SECURE 安全模式的分级缓解体系。本讲把镜头转向**调试期检测**：当你怀疑程序有越界写、双重释放、use-after-free 时，mimalloc 的 Debug 构建与 MI_GUARDED 构建各自能帮你抓到什么、怎么抓、代价是什么。

学完本讲你应该能够：

1. 说出 MI_DEBUG 三级断言（`mi_assert` / `mi_assert_internal` / `mi_assert_expensive`）各自的生效条件与典型检查范围。
2. 画出 `mi_padding_t` 在块尾的布局，解释 canary + delta + 0xDE 填充字节如何实现「byte 级越界检测」与「双重释放检测」，以及检测失败时 free 被中止的语义。
3. 解释 MI_GUARDED 的采样判定算法（count 先行递减、尺寸窗口后判）、guard page 的放置数学，以及 rate / seed / min / max / precise 五个调参项的确切含义。
4. 独立完成一个「1 字节越界」用例：在 debug 构建下由 padding 校验在 free 时报出，在 guarded 构建下由 OS 守卫页在**写操作的当下**当场捕获。

## 2. 前置知识

- **块与页**（u3-l1/u3-l2）：mimalloc 页只装一种 size class 的等长块；块没有独立头，空闲时块的前 8 字节复用为 `mi_block_t::next` 串成 free list。
- **MI_PADDING 已经在路上出现过**（u3-l3 曾提到「debug 构建因每块 +8 字节 padding 会使边界尺寸跳档」）——本讲就讲这 8 字节的真身。
- **free 的分流**（u5-l1/u5-l2）：free 先由指针反查页，再按 `xthread_id` 异或结果分为本地释放（`mi_free_block_local`）与跨线程释放（`mi_free_block_mt`）。
- **guard page**（u9-l1）：把一段已 commit 的内存用 `mprotect`/`VirtualAlloc` 改为不可访问，任何读写立即触发 SIGSEGV。u9-l1 讲的是 secure 模式给**页元数据**加守卫；本讲 MI_GUARDED 是给**用户对象**尾部按采样率加守卫，两者机制同源（`_mi_os_protect`）、用途不同。
- **错误上报通道**：`_mi_error_message(err, fmt, ...)` 打印消息（默认最多 32 条，`mi_option_max_errors`）并调用错误处理器；默认处理器在 debug/secure 构建下对 `EFAULT`（越界/元数据损坏类错误）调用 `abort()`，对 `EAGAIN`（双重释放）只报告不中止。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
| --- | --- |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 功能宏级联（MI_DEBUG→MI_PADDING→MI_GUARDED…）、`mi_padding_t` 定义、theap 的 guarded 采样字段、0xD0/0xDE/0xDF 填充字节常量 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | 分配侧：padding 的写入（canary/delta/填充）、guarded 分配入口 `_mi_theap_malloc_guarded` 与守卫页放置 |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放侧：padding 校验 `mi_verify_padding`、双释放检测、无效指针检测、释放后投毒、守卫页撤销 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | 断言三级宏、采样判定 `mi_theap_malloc_use_guarded`、`MI_BLOCK_TAG_GUARDED`、canary 编解码 |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | guarded 五个选项的默认值、`_mi_error_message` 与默认错误处理 |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | 采样参数从选项灌入 theap（含随机种子） |
| [src/os.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c) | `_mi_os_protect` / `_mi_os_unprotect`：守卫页的底层原语 |
| [test/test-wrong.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c) | 官方「错误用法」样本：越界读/写、双重释放、use-after-free、未初始化读 |
| [CMakeLists.txt](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt) | MI_DEBUG/MI_GUARDED/MI_PADDING 构建开关与联动 |

## 4. 核心概念与源码讲解

### 4.1 模块一：debug 构建的检查体系——宏级联与断言三级

#### 4.1.1 概念说明

mimalloc 的「debug 模式」不是一个运行时开关，而是**一组编译期功能宏的级联**：`MI_DEBUG` 是总闸，它自动点亮 `MI_PADDING`（块尾填充检测）、`MI_PADDING_CHECK_BYTES`（byte 级填充校验）、`MI_ENCODE_FREELIST`（free list 指针编码）、`MI_STAT=2`（细粒度统计）和 `MI_GUARDED`（守卫页采样）。断言则分三级，分别绑定到 `MI_DEBUG` 的 1/2/3 档，逐级加码、release 下全部削为空宏零开销。

#### 4.1.2 核心流程

```text
cmake -DCMAKE_BUILD_TYPE=Debug
  └─ MI_DEBUG=INTERNAL → 宏 MI_DEBUG=2            (CMakeLists.txt)
       ├─ MI_PADDING=1            (types.h:96-98)
       ├─ MI_PADDING_CHECK_BYTES=1 (types.h:101-103)
       ├─ MI_ENCODE_FREELIST=1    (types.h:108-110)
       ├─ MI_STAT=2               (types.h:81-87)
       └─ MI_GUARDED=1            (types.h:90-92 与 CMakeLists.txt:396-403 双路自动开启)

MI_DEBUG 档位 → 断言：
  1  → mi_assert             （基本契约：指针非空、对齐、计数关系…）
  2  → + mi_assert_internal   （内部不变式：页/链表一致性，cmake Debug 默认档）
  3  → + mi_assert_expensive  （昂贵校验：如逐字节验证 zero 分配确实全零）
```

#### 4.1.3 源码精读

**宏级联**。[include/mimalloc/types.h:68-103](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L68-L103) 是整条链路的源头：`MI_DEBUG` 未定义时，若处于非 release 且未定义 `NDEBUG` 则默认为 2（所以你把 mimalloc 源码直接编进自己的 debug 工程也会自动得到全套检查）；`MI_PADDING` 在 `MI_SECURE>=3 || MI_DEBUG>=1`（或 valgrind/ASAN/ETW 跟踪构建）时自动置 1；`MI_GUARDED` 在 `MI_DEBUG && !NDEBUG && !MI_OPT_FREE_SMALL` 时自动置 1。

**断言三级**。[include/mimalloc/internal.h:347-365](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L347-L365)：三级宏都落到自家的 `_mi_assert_fail`（不分配内存、直接打印），`mi_assert` 要求 `MI_DEBUG>0`，`mi_assert_internal` 要求 `>1`，`mi_assert_expensive` 要求 `>2`。典型用例：

- `mi_assert`（1 级）：如 [src/free.c:341](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L341) 校验 `mi_free_aligned` 的指针对齐——用户可感知的 API 契约。
- `mi_assert_internal`（2 级）：如 [src/free.c:492](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L492) 断言 `page->reserved>=16`、[src/alloc.c:58-59](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L58-L59) 断言弹出的块确实属于本页——分配器内部不变式。
- `mi_assert_expensive`（3 级）：如 [src/alloc.c:61-65](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L61-L65) 逐字节验证 `free_is_zero` 页弹出的块确实全零。此外源码中还有 `MI_DEBUG>3` 的检查（如 [src/alloc.c:154-158](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L154-L158)），需手动 `-DMI_DEBUG=4`，cmake 最高只到 3。

**构建侧联动**。[CMakeLists.txt:373-403](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L373-L403)：`MI_DEBUG=DEFAULT` 时，构建目录是 Debug 类型则取 INTERNAL(2)，否则 OFF；并且只要 MI_DEBUG 开着，**MI_GUARDED 会被强制开启**（第 396-398 行）。[CMakeLists.txt:405-412](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L405-L412) 则允许 `MI_PADDING=ON` / `MI_NO_PADDING=ON` 显式控制填充。

**调试填充字节**。[include/mimalloc/types.h:793-801](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L793-L801) 定义了三个魔数：`MI_DEBUG_UNINIT=0xD0`（分配时未初始化填充）、`MI_DEBUG_FREED=0xDF`（释放后投毒）、`MI_DEBUG_PADDING=0xDE`（padding 区填充）。它们让「读了未初始化内存」「读了已释放内存」在调试器里一眼可辨——0xDFDFDFDF 一定是 use-after-free。

#### 4.1.4 代码实践

1. **实践目标**：直观感受断言分级与调试填充的存在。
2. **操作步骤**：
   - `mkdir -p out/debug && cd out/debug && cmake ../.. -DCMAKE_BUILD_TYPE=Debug && make -j8`（注意 cmake 会打印 `Enable MI_GUARDED (since MI_DEBUG is enabled)`）；
   - 统计内部断言规模：`grep -c mi_assert_internal ../../src/free.c ../../src/alloc.c`；
   - 再构建一个 release 版（`out/release`），对 `test-wrong.c` 风格程序分别链接两个版本运行。
3. **需要观察的现象**：debug 构建输出错误消息并可能中止；release 构建对同样的错误静默；`grep -c` 给出几十处内部断言。
4. **预期结果**：debug 版运行含越界写的程序时报 `buffer overflow in heap block ...`（详见 4.2）；release 版正常运行无输出。具体输出文本**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_assert_expensive` 在 cmake 里默认永远不生效？

**答案**：cmake 的 MI_DEBUG 档位最高是 FULL=3（[CMakeLists.txt:385-390](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L385-L390)），而 `mi_assert_expensive` 要求 `MI_DEBUG>2` 即 3 档全开才生效；想再往上（源码里存在 `MI_DEBUG>3` 分支）只能手动 `-DMI_DEBUG=4` 编译。昂贵断言的定位就是「偶尔全量查一次」的工具，不该出现在任何默认构建里。

**练习 2**：把 mimalloc 源码直接 `#include` 进自己的工程（非 NDEBUG、未定义 MI_BUILD_RELEASE），会自动得到哪些检查？

**答案**：由 [types.h:72-78](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L72-L78) 的默认规则，`MI_DEBUG` 自动取 2，于是 MI_PADDING、MI_PADDING_CHECK_BYTES、MI_ENCODE_FREELIST、MI_STAT=2 全部点亮；MI_GUARDED 也因 `MI_DEBUG && !NDEBUG` 自动开启——但注意其默认采样率为 0（见 4.3），需要环境变量或选项显式打开才真正放守卫页。

### 4.2 模块二：padding 校验——byte 级越界、双重释放与无效指针检测

#### 4.2.1 概念说明

`MI_PADDING` 在**每个块尾部**附加一个 8 字节的 `mi_padding_t` 小结构，紧挨着用户可用区的末尾。它同时承担三件事：

1. **byte 精确的 `mi_usable_size`**：记录 `delta`（块大小与用户请求的差），让 usable size 精确等于请求值而非 size class 的块大小。
2. **越界写检测**：用户区结束后的填充字节涂成 0xDE，free 时复查；canary 被砸则说明越界写穿到了块尾。
3. **双重释放检测**：首次 free 成功后把 canary 改写成墓碑值 `0x00DEAD00`，再次 free 时比对即中。

检测发生在 **free 时**（惰性检测）——这与 guarded 模式的「写操作当下硬件捕获」（4.3）是本讲最重要的对照轴。

#### 4.2.2 核心流程

块尾布局（设页的块大小为 \( B \)，用户请求 \( n \) 字节；debug 下分配请求实际是 \( n+8 \)）：

```text
block ──► [ 用户可用区 n 字节 ][ 填充区 delta 字节 ][ mi_padding_t 8字节 ]
           ▲                                    ▲
           │ 0xDE 涂满前 min(delta,16) 字节      ├─ canary: 指针经页 key 编码
           └─ usable_size = B − delta = n        └─ delta:  B − n
```

关键关系式：

\[ \text{delta} = B - n,\qquad \text{usable} = B - \text{delta} = n,\qquad \text{检查窗} = [n,\; n + \min(\text{delta}, 16)) \]

free 时的判定流程：

```text
mi_check_padding_on_free(page, block)
  ├─ 是 guarded 块？ → 直接给 usable = B − OS页大小，返回 true（守卫页已代劳，见 4.3）
  └─ mi_verify_padding
       ├─ 解码 padding：canary 是否等于重新编码值，且 delta ≤ B？
       │    ├─ 不等且 canary == 0x00DEAD00 → 双重释放 → EAGAIN 报错（不中止进程）
       │    └─ 不等（其他值）             → 越界写穿 → EFAULT 报错（debug 构建默认 abort）
       └─ 逐字节检查填充区前 min(delta,16) 字节是否仍为 0xDE
            └─ 第 i 字节被改 → 报 "write after (n+i) bytes"
  └─ 校验失败 → free 直接 return：块不入 free list（故意泄漏，防止损坏扩散）
```

#### 4.2.3 源码精读

**结构定义**。[include/mimalloc/types.h:544-555](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L544-L555)：

```c
typedef struct mi_padding_s {
  uint32_t canary; // encoded block value to check validity of the padding (in case of overflow)
  uint32_t delta;  // padding bytes before the block. ...
} mi_padding_t;
```

这就是那「+8 字节」的真身；无 MI_PADDING 时 `MI_PADDING_SIZE` 为 0，一切相关代码消失。

**分配侧写入**。[src/alloc.c:104-120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L104-L120)：算出 `delta` 后写 `padding->canary = mi_ptr_encode_canary(page,block,page->keys)`、`padding->delta = delta`，并把 `padding - delta` 开始的至多 `MI_MAX_ALIGN_SIZE`（16，[types.h:38](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L38)）字节涂成 0xDE。前置条件在 [src/alloc.c:150-151](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L150-L151)：小对象分配请求被抬大为 `size + MI_PADDING_SIZE`，保证 padding 装得进同一个 size class 的块。另外 [src/alloc.c:90-92](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L90-L92) 在非零分配时用 0xD0 涂满整块，暴露「读未初始化内存」。

**canary 编码与墓碑**。[include/mimalloc/internal.h:1226-1243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1226-L1243)：canary 取指针编码值的低 32 位并**清掉最低字节与 bit 9**——最低字节清零是为防止「顺带多读 1 字节」被误判（issue #951），bit 9 专留给墓碑：`mi_ptr_encode_canary_freed()` 返回 `0x00DEAD00`，任何合法 canary 都不可能等于它。

**释放侧校验**。[src/free.c:619-632](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L619-L632) 的 `mi_page_decode_padding` 重算 canary 并比对；调用方传了 `double_free` 出参时，失败会顺带判断是否墓碑，成功则**当场把 canary 改写成墓碑**（首次 free 的「顺手写墓碑」）。[src/free.c:687-711](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L687-L711) 的 `mi_verify_padding` 再逐字节复核填充区，`*wrong = bsize - delta + i` 精确记录第几个字节被改。

**报错与中止语义**。[src/free.c:713-733](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L713-L733)：双重释放走 `_mi_error_message(EAGAIN, "double free detected of heap block %p with size %zu\n", ...)`；越界走 `EFAULT, "buffer overflow in heap block %p of size %zu: write after %zu bytes\n"`。两种情况都 `return false`，而调用方 [src/free.c:32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L32)（本地路径）与 [src/free.c:66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L66)（跨线程路径）都是「校验失败立即 return」——**这个块不会被释放**，宁可泄漏也不把可能已损坏的块挂回链表。错误最终经 [src/options.c:596-609](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L596-L609) 上报，默认处理器 [src/options.c:566-578](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L566-L578) 在 `MI_DEBUG>0` 或 `MI_SECURE>0` 时对 EFAULT 直接 `abort()`；EAGAIN 不中止。可用 [mimalloc.h:191](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L191) 的 `mi_register_error` 换掉默认行为。

**释放后投毒**。校验通过后，[src/free.c:39-42](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L39-L42)（本地）与 [src/free.c:73-78](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L73-L78)（跨线程）把块涂成 0xDF（至多 1MiB）——use-after-free 的读会拿到 0xDFDFDFDF，调试器里一目了然。

**无效指针检测**。[src/free.c:172-214](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L172-L214) 的 `mi_validate_ptr_page_nonnull`：先查指针未按机器字对齐（EINVAL "invalid (unaligned) pointer"），再在 `MI_PAGE_META_IS_ALIGNED` 主路径下用 `_mi_checked_ptr_page` 复核 page map 中确有此页（EINVAL "invalid pointer"），并用 `mi_assert(cpage==page)` 交叉验证对齐反查与 page map 一致。此外 [src/free.c:310-337](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L310-L337) 的 `mi_free_size` 在 debug 下会比对传入 size 与 usable size，[src/free.c:352-361](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L352-L361) 的 `mi_cfree` 则是「先验证再释放」的公开变体。

**历史注脚**：旧的「遍历三条 free list 找双释放」实现 [src/free.c:559-611](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L559-L611) 已被 `MI_CHECK_DOUBLE_FREE` 弃用——[free.c:553-557](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L553-L557) 的节标题注释「Deprecated: double free is checked with padding now」与 [types.h:112-115](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L112-L115) 的注释互相印证：线性扫链是 O(capacity) 的，如今由 padding 墓碑 O(1) 完成。

#### 4.2.4 代码实践

1. **实践目标**：用 debug 构建的 padding 校验抓出一个 1 字节越界写。
2. **操作步骤**：
   - 按 4.1.4 构建 `out/debug`；
   - 编写下面的用例（**示例代码**，仿照 [test/test-wrong.c:72-88](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L72-L88)）：

     ```c
     // overflow1.c —— 示例代码
     #include <stdio.h>
     #include <mimalloc.h>
     int main(void) {
       char* p = (char*)mi_malloc(100);   // debug 下实际按 108 字节选 size class
       for (int i = 0; i < 100; i++) p[i] = 'a';  // 合法写满
       p[100] = 'X';                      // 越界恰好 1 字节，落入 0xDE 填充区
       mi_free(p);                        // 校验在此刻发生
       printf("freed\n");                 // debug 构建下预期走不到这里（abort）
       return 0;
     }
     ```

   - 编译运行：`gcc -g overflow1.c -o overflow1 -I../../include libmimalloc-debug.a -lpthread && ./overflow1`。
3. **需要观察的现象**：stderr 打印 `buffer overflow in heap block 0x... of size 100: write after 100 bytes`，随后进程 `abort()`（SIGABRT）。
4. **预期结果**：如上；`write after 100 bytes` 中的 100 正是 `*wrong = bsize - delta + i` 在 i=0 处的取值——报告精确到被砸的那个字节。若把 `p[100]='X'` 换成 `p[107]='X'`（砸到 canary 本体），同样报 buffer overflow，但 wrong 会是块大小。程序输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：请求 `mi_malloc(64)` 时 delta 一定是多少？这时 1 字节越界还能被填充字节检测到吗？

**答案**：不一定为 0，取决于该请求落入的 size class 块大小 B：delta = B − 64。若 64 恰好是块大小（在 mimalloc 尺寸表中前 8 档是精确尺寸，64 是其中之一），则 delta = 0、检查窗为空，1 字节越界会直接砸中 padding 结构的 canary 字段，仍会被检测（报 wrong = B）；若 B > 64 则落入 0xDE 检查窗被字节级检出。两条路都通向 `mi_verify_padding` 失败，只是报告的字节数不同。

**练习 2**：为什么检测到越界后 mimalloc 选择「不释放这个块」而不是修复后继续？

**答案**：见 [src/free.c:32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L32)——校验失败立即 return。一个已被越界写穿透的块，其相邻元数据（padding、乃至邻块）可能都已损坏，把它挂回 free list 等于把脏数据注入分配器内部状态，可能引发更隐蔽的崩溃。泄漏一个块是可容忍、可观测的代价。

**练习 3**：填充字节检查窗为什么限制在 `min(delta, MI_MAX_ALIGN_SIZE)` 而不是整个 delta？

**答案**：见 [src/alloc.c:116](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L116) 与 [src/free.c:698](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L698)：分配时只涂 16 字节、释放时也只查 16 字节，首尾对称。绝大多数现实中的越界写紧贴用户区末尾（off-by-one、按 usable size 多写），16 字节窗口已覆盖；而大 delta（如 size class 跳档造成的几十字节空隙）全量涂写+校验会增加每次分配/释放的内存写入量。窗口之外的写只有砸到 canary 才会被发现——这是性能与覆盖率的折中。

### 4.3 模块三：MI_GUARDED——按采样率在对象后放 OS 守卫页

#### 4.3.1 概念说明

padding 校验是**事后**的（free 时才知道）。MI_GUARDED 把检测点提前到**写操作发生的瞬间**：在 sampled 对象尾部放一个真正的 OS 守卫页（`mprotect` 为不可访问），越界写当场 SIGSEGV，调试器直接停在肇事指令上。代价是每个被守卫的对象至少占一个 OS 页（4KiB）做守卫 + 对齐开销（合计约 8KiB）和一次系统调用，因此默认按采样率随机抽取对象，而不是全员守卫。readme 明确建议：采样率模式下性能接近 release，可以**在生产环境常开**来抓潜伏的越界 bug（[readme.md:448-451](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L448-L451)）。

#### 4.3.2 核心流程

**采样判定**（挂在 theap 上、每线程独立，[internal.h:1143-1168](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1143-L1168)）：

```text
每次分配（快/慢路径入口处）:
  count = theap->guarded_sample_count
  if count != 0:                      # 绝大多数走这里：一次读改写 + 一次比较
      theap->guarded_sample_count = count - 1
      return 不守卫
  # count 减到 0 才查选项（避免热路径查选项表）:
  rate == 0            → 不守卫（采样关闭）
  min ≤ size ≤ max     → 守卫本对象；count 重置为 rate
  尺寸不在窗口         → 不守卫；count 置 1（下一个符合窗口的分配立即守卫）
```

两个精妙处：其一，rate 为 0 时 count 从 0 下溢成 SIZE_MAX 再一路递减，「很久才会再走到慢分支」，且不写共享的空 theap（[internal.h:1144-1150](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1144-L1150) 注释）；其二，初始 count 由种子随机化为 `(seed % rate) + 1`，避免所有线程同步在第 rate 次同时守卫（[theap.c:194-199](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L194-L199)）。

**守卫页放置数学**（[src/alloc.c:913-949](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L913-L949)）：

\[ \text{obj\_size} = \begin{cases} n & \text{precise 模式} \\ \lceil n \rceil_{16} & \text{默认（对齐到 MI\_MAX\_ALIGN\_SIZE=16）} \end{cases} \]

\[ \text{bsize} = \lceil \lceil \text{obj\_size} \rceil_{16} + 8 \rceil_{16},\qquad \text{req\_size} = \lceil \text{bsize} + P \rceil_{P} \quad (P = \text{OS 页大小}) \]

即：多要一个 OS 页，把最后那页 `mprotect` 成守卫页，用户指针 `p` 放在守卫页**正前方**。于是写 `p[obj_size]` 起立即撞墙，`mi_usable_size(p) = obj_size`。

**释放侧**：free 时若识别出是 guarded 块（`offset ≥ 8 && block->next == ~0`），先 `_mi_os_unprotect` 撤守卫，再走常规释放；整堆销毁时 `_mi_page_unguard_all` 一次性解除整页保护。

#### 4.3.3 源码精读

**五个选项**。[src/options.c:154-159](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L154-L159) 定义、[mimalloc.h:495-499](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L495-L499) 公开：

| 选项 / 环境变量 | 默认 | 含义 |
| --- | --- | --- |
| `guarded_sample_rate`（`MIMALLOC_GUARDED_SAMPLE_RATE`） | release+guarded 构建为 4000，否则 0 | 每 N 个窗口内分配守卫 1 个；0 关闭、1 全守卫 |
| `guarded_sample_seed`（`MIMALLOC_GUARDED_SAMPLE_SEED`） | 0（随机） | 固定种子以**可复现地重跑**同一次触发 |
| `guarded_min`（`MIMALLOC_GUARDED_MIN`） | 0 | 窗口下界（**取整后的对象尺寸**） |
| `guarded_max`（`MIMALLOC_GUARDED_MAX`） | 1GiB | 窗口上界 |
| `guarded_precise`（`MIMALLOC_GUARDED_PRECISE`） | 0 | 不做 16 字节取整，紧贴守卫页放，连 1 字节越界都撞墙（牺牲 C 对齐保证） |

默认值分叉见 [src/options.c:79-85](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L79-L85)：`MI_GUARDED && !MI_DEBUG` 时默认 4000（生产采样），debug 构建默认 0（守卫页会干扰你正在调的别的东西，要用再开）。参数在 theap 初始化时灌入：[src/theap.c:207-214](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L207-L214)；运行期也可用 [mimalloc.h:421-424](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc.h#L421-L424) 的 `mi_theap_guarded_set_sample_rate` / `mi_theap_guarded_set_size_bound` 按 theap 调整。

**采样入口**。小对象与通用分配的最前面各有一个采样分支：[src/alloc.c:143-147](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L143-L147)、[src/alloc.c:165-172](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L165-L172)——命中即改道 `_mi_theap_malloc_guarded`。注意分支放在**最前面**，此时还没查 pages_free_direct，说明采样判定被认为足够便宜（一次递减+比较）才敢放在每条分配路径上。

**守卫页放置**。[src/alloc.c:868-911](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L868-L911) 的 `mi_block_ptr_set_guarded`：先把页标记 `has_interior_pointers`（用户指针在块内偏移处，free 需按块大小反推块起点，走 u4-l4 讲过的 interior-pointer 路径）；再把 `block->next = MI_BLOCK_TAG_GUARDED`（`~0`，[internal.h:1128-1129](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1128-L1129)——与对齐分配的 tag=0 区分，复用空闲块首字当「身份牌」）；然后计算 `guard_page = block + block_size - P`，若内存未 pin 住则 `_mi_os_protect` 置为不可访问；最后把 `p` 放在 `guard_page - obj_size` 处（偏移超过 `MI_PAGE_MAX_OVERALLOC_ALIGN` 时钳制并放弃紧贴）。守卫页能整页对齐的前提是块本身 OS 页对齐，这由 arena 的新鲜页分配保证（[src/alloc.c:884-887](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L884-L887) 注释）。

**守卫页底层**。[src/os.c:690-713](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L690-L713)：`mi_os_protectx` 保守按页对齐后调 `_mi_prim_protect`（unix 下即 `mprotect`）。大页/巨页是 pin 住的无法 protect，所以 [src/options.c:195-202](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L195-L202) 在采样率 >0 时**主动关闭** `allow_large_os_pages` 并告警。

**释放侧撤销**。free 分流前先识别：[src/free.c:128-144](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L128-L144) 的 `mi_block_check_unguard`（判定函数在 [internal.h:1132-1140](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1132-L1140)），命中则 [src/free.c:789-803](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L789-L803) 的 `mi_block_unguard` 调 `_mi_os_unprotect` 撤守卫。guarded 块**跳过 padding 校验**（[src/free.c:713-718](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L713-L718)：is_guarded 分支直接给 usable = 块大小 − OS 页大小）——守卫页期间任何越界写已经当场崩了，无需复查；且块首字被 tag 占用、用户指针有偏移，padding 的布局假设也不再成立。整页销毁时 [src/free.c:806-811](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L806-L811) 的 `_mi_page_unguard_all` 一次 unprotect 整个已提交区间（无从知道哪些块被守卫过，干脆全解）。

**两个边界**。对齐分配若自身要求大对齐，改为「过度分配 + 对齐」复用 guarded 通道（[src/alloc-aligned.c:40-48](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L40-L48)）；反之需要内部分配时用 [src/alloc-aligned.c:51-61](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc-aligned.c#L51-L61) 的包装临时把 rate 置 0，防止嵌套守卫。另外 MI_GUARDED 与 `mi_free_small` 的免查表快路径互斥（[src/free.c:268-292](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L268-L292) 编译期 `#warning` 后退化为普通 `mi_free`），这也是 types.h:90 中 `!MI_OPT_FREE_SMALL` 条件的由来。

**官方用法佐证**：测试基建自己就是这么用的——[CMakeLists.txt:965-966](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L965-L966) 在 MI_GUARDED 构建下给每个测试套上 `MIMALLOC_GUARDED_SAMPLE_RATE=1` 运行，即全量守卫回归。

#### 4.3.4 代码实践

1. **实践目标**：复现同一个 1 字节越界，这次由 guard page 在**写的当下**捕获（而非 free 时）。
2. **操作步骤**：
   - 构建守卫版：`mkdir -p out/guarded && cd out/guarded && cmake ../.. -DMI_GUARDED=ON -DCMAKE_BUILD_TYPE=Release && make -j8`（用 Release 是为了证明这套机制不需要 debug 设施）；
   - 复用 4.2.4 的 `overflow1.c`，但把越界写改成紧贴守卫页的写法（**示例代码**）：

     ```c
     char* p = (char*)mi_malloc(100);   // 默认取整: obj_size = 112
     memset(p, 'a', 100);
     p[112] = 'X';                      // obj_size 处即守卫页第一字节
     mi_free(p);
     ```

   - 运行：`MIMALLOC_GUARDED_SAMPLE_RATE=1 ./overflow1_g`（rate=1 → 窗口内全部守卫；release+guarded 构建其实默认就是 4000，显式设 1 更直观）；
   - 再对比 precise：`MIMALLOC_GUARDED_SAMPLE_RATE=1 MIMALLOC_GUARDED_PRECISE=1 ./overflow1_g`，并把越界写改成 `p[100]='X'`。
3. **需要观察的现象**：程序在 `p[112]='X'` 这一行收到 SIGSEGV（`dmesg`/shell 显示 Segmentation fault），根本到不了 `mi_free`；开 PRECISE 后 `p[100]='X'` 也同样当场段错误。
4. **预期结果**：如上。用 gdb 运行时栈顶恰好停在肇事写指令——这就是「写时捕获」相对 padding「free 时捕获」的价值。默认（非 precise）模式下 `p[100]`~`p[111]` 落在 16 字节取整的缝隙里**不会**触发（这也解释了为何有 PRECISE 选项，[readme.md:460-462](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L460-L462)）。崩溃行为**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：生产服务想常开 guarded 采样，又担心内存与系统调用开销，推荐的参数组合是什么？

**答案**：release + `-DMI_GUARDED=ON`（默认 rate=4000，见 [options.c:79-85](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L79-L85)），min/max 保持默认或收紧到可疑尺寸附近。这样每线程每 4000 个窗口内分配才放一个守卫页（约 8KiB + 一次 mprotect），平摊开销极小；一旦线上崩溃，把崩溃报告中的尺寸填进 `MIMALLOC_GUARDED_MIN/MAX`、设 `RATE=1`、用 `SEED` 固定起点，即可在测试环境确定性复现。

**练习 2**：为什么 guarded 块的 free 不再走 padding 校验，而 `mi_free_small` 的快路径在 MI_GUARDED 构建下干脆被编译期禁用？

**答案**：前者见 [src/free.c:713-718](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L713-L718)——被守卫期间任何越界写早已当场 SIGSEGV，到 free 时块不可能「带伤存活」，复查 padding 没有意义；且用户指针相对块起点有偏移、块首字是 tag，padding 布局假设不成立。后者见 [src/free.c:268-292](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L268-L292)：`mi_free_small` 靠「指针向下对齐到小页边界」直接找页元数据，但 guarded 指针在块内偏移处、页可能含 interior 指针，必须走通用反查路径，所以编译期 `#warning` 并退化为 `mi_free`。

**练习 3**：`mi_theap_malloc_use_guarded` 把「查尺寸窗口」放在 count 减到 0 之后，而不是每次分配都查，这个顺序还带来一个微妙的正确性行为，是什么？

**答案**：见 [internal.h:1157-1166](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1157-L1166)：count 归零但尺寸不在 [min,max] 窗口内时，count 被置为 1 而非 rate——即「窗口外的分配不消耗采样名额」，下一个落在窗口内的分配会立即被守卫。若不加这个回退，大量小分配会把计数器消耗光，真正关心的尺寸反而长期采不到。

## 5. 综合实践

把三份检测报告放在一起对照。构建三个版本的库（可放 `out/debug`、`out/release`、`out/guarded` 三个目录），用同一个 `wrong-all.c`（**示例代码**，可直接改造自 [test/test-wrong.c:55-99](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L55-L99)，按 4.2.4 方式链接）依次运行：

```c
// wrong-all.c —— 示例代码：四类错误各来一次
#include <stdio.h>
#include <mimalloc.h>
int main(void) {
  long* q = (long*)mi_malloc(sizeof(long)); // 8 字节
  q[1] = 43;                 // A. 越界写（+8 字节处）
  mi_free(q);
  mi_free(q);                // B. 双重释放
  printf("after double free\n");
  char* c = (char*)mi_malloc(3);
  printf("over: %d\n", c[4]); // C. 越界读 / D. 读未初始化（0xD0 或垃圾）
  return 0;
}
```

对每个版本记录：程序在哪一行停止、stderr 打了什么、退出信号是什么。然后写一份对照表并回答：

| 版本 | A 越界写 | B 双重释放 | C/D 越界读 |
| --- | --- | --- | --- |
| release | 静默损坏 | 静默 | 读到相邻块数据 |
| debug（padding） | free 时报 EFAULT + abort | 报 EAGAIN 不中止 | 0xD0（未初始化涂毒） |
| guarded + RATE=1 | 写 q[1] 是否当场 SIGSEGV？（注意 8 字节请求会被取整到 16，q[1] 在缝隙内——想当场崩需 `PRECISE=1` 或写 q[2]） | 报 EAGAIN 不中止 | 同左 |

预期结论：三个版本正好覆盖「何时发现」（永不 / free 时 / 写时）与「发现得多准」（静默 / 精确到字节 / 精确到指令）的两个维度；guarded 默认取整留缝、padding 专补缝隙内的越界，两者互补。全部运行输出**待本地验证**。

## 6. 本讲小结

- 「debug 模式」是 `MI_DEBUG` 驱动的编译期级联：自动点亮 MI_PADDING、MI_PADDING_CHECK_BYTES、MI_ENCODE_FREELIST、MI_STAT=2 与 MI_GUARDED；断言分 `mi_assert`(>0) / `mi_assert_internal`(>1) / `mi_assert_expensive`(>2) 三级，release 全部为空宏。
- `mi_padding_t`（canary + delta，8 字节）贴在每个块尾：delta 让 `mi_usable_size` byte 精确，canary 检测写穿，0xDE 填充窗口（前 min(delta,16) 字节）检测紧邻越界；首 free 顺手写墓碑 `0x00DEAD00`，双释放由它 O(1) 检出（取代旧的三链表线性扫描）。
- 校验失败的 free **直接中止、不释放该块**（宁泄漏不扩散）；错误经 `_mi_error_message` 上报，EFAULT 在 debug/secure 构建默认 `abort()`，EAGAIN 只报告。
- MI_GUARDED 用真 OS 守卫页把检测提前到**写操作的当下**：采样判定是热路径上的一次计数递减，归零才查 rate 与尺寸窗口；放置数学为「obj_size 取整到 16（precise 则不取整）+ 块首字打 `~0` tag + 尾页 mprotect + 指针紧贴守卫页」。
- 调参五件套：`RATE`（0 关 / 1 全守卫 / N 采样）、`SEED`（可复现）、`MIN`/`MAX`（尺寸窗口，按取整后尺寸）、`PRECISE`（连 1 字节越界都撞墙，牺牲对齐保证）；rate>0 时大页被自动禁用，因为 pin 住的内存无法 mprotect。
- padding（free 时、byte 精确、零额外系统调用）与 guarded（写时、指令精确、约 8KiB/对象 + 一次 mprotect）是互补的两层检测网；官方测试在 MI_GUARDED 构建下正是用 `MIMALLOC_GUARDED_SAMPLE_RATE=1` 全量守卫回归的。

## 7. 下一步学习建议

本讲之后，单元九还剩三讲：**u9-l3 统计系统**（`mi_stats_t`、按 heap/theap/subproc 粒度取数与 JSON 导出——本讲多次用到的 `MIMALLOC_SHOW_STATS` 的底层就在那里）；**u9-l4 堆遍历**（`mi_heap_visit_blocks` 如何沿页队列与 free list 推断块存活状态，是 GC 集成与泄漏工具的基础）；**u9-l5 工具链与基准**（MI_TRACK_VALGRIND/ASAN/ETW 构建——本讲的 test-wrong.c 头部注释已预告了它的 valgrind/asan 用法，那一讲会把三条插桩路径与性能评估方法讲全）。若想继续深挖本讲的机制，建议重读 [src/free.c:614-742](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L614-L742)（padding 全家桶）并对照 u9-l1 的 free list 编码（[internal.h:1173](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1173) 起的 encode/decode 族）——canary 正是那套编码的 32 位裁剪版。
