# 安全模式：加密 free list、guard page 与双重释放检测

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `MI_SECURE` 不是单一开关，而是 0~5 的**分级**缓解体系，并说出每一级打开了哪些宏。
2. 讲清 free list 指针编码 \(((p \oplus k_2) \lll k_1) + k_1\) 的设计动机：为什么单纯 `p^k1` 不够。
3. 说明 padding canary 如何用一次「写墓碑」操作同时完成缓冲区溢出检测与 double-free 检测。
4. 解释 guard page「set = decommit、reset = commit」的实现技巧，以及它保护的是**元数据**还是**用户数据**。
5. 独立完成一次 release 与 secure 构建的对比实验，并能从源码中指出是哪一行检查报告了错误。

## 2. 前置知识

### 2.1 分配器为什么会被攻击

回顾 u3-l2：mimalloc 的空闲块是一个侵入式链表，块的头 8 字节在空闲时复用为 `mi_block_t.next` 指针。经典的堆利用思路是：

1. 用缓冲区溢出改写相邻空闲块的 `next` 指针，让下一次 `malloc` 返回一个指向**任意地址**（比如某个函数指针表）的「块」；
2. 或者 double free 让同一个块两次进入链表，造成两个指针指向同一内存，再借类型混淆改写元数据。

安全的分配器无法阻止业务代码写坏自己的数据，但可以做到两件事：

- **让攻击者无法伪造内部指针**（编码 / 加密）；
- **让越界访问在到达敏感结构之前就崩掉**（guard page）。

readme 明确提醒：这些都是缓解（mitigations）而非保证（[readme.md:426](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L426)）。

### 2.2 需要回忆的前几讲结论

- **三条 free list**（u3-l2）：`free` / `local_free` / `xthread_free`，空闲块的 `next` 字段裸露在用户可写的内存里——这正是要加密的对象。
- **page map**（u3-l4）：`mi_free` 从裸指针反查 `mi_page_t`；secure 模式下这次反查会走「带校验」版本。
- **错误报告通道**（u2-l3 / u5-l1）：所有检测失败都汇聚到 `_mi_error_message(err, ...)`，默认 handler `mi_error_default` 决定是「打印后继续」还是 `abort()`。
- **MI_DEBUG 分级**（u1-l2）：debug 构建自带大量检查；本讲会反复对比「debug 检查」与「secure 检查」的交集与差异。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [include/mimalloc/types.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h) | 全部安全相关的**编译期宏开关**与 `mi_padding_t`、`keys[]` 字段定义 |
| [src/random.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c) | ChaCha20 伪随机数发生器：所有 key / cookie / 随机化的熵源 |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | theap 级 PRNG 的初始化与分叉 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | 指针编码/解码算法、canary 算法、`mi_block_next` 损坏检测、free 反查分发 |
| [src/page.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c) | 页 keys 的生成、随机化 free list 初始化、随机扩展 |
| [src/alloc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c) | 分配时写入 padding canary |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | 释放时的全部检测：padding 校验、double-free、损坏链表 |
| [src/os.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c) | guard page 原语（set/reset）、大块内存地址随机化 |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | guard page 的使用方：arena 元数据区、mimalloc 页 |
| [src/options.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c) | 错误报告与 abort 策略、`_mi_block_next_is_corrupted` |

## 4. 核心概念与源码讲解

### 4.1 分级开关：MI_SECURE 不是布尔值

#### 4.1.1 概念说明

u1-l2 提过 cmake 的 `MI_SECURE=ON/FULL` 会被翻译成 C 宏。真实情况比「开/关」精细得多：宏 `MI_SECURE` 是一个 0~5 的整数，每升一級多打开一组缓解，代价也递增。理解本讲其余模块的钥匙就是这张级联表。

#### 4.1.2 核心流程

构建期链路（承接 u1-l2 的「cmake 选项 → C 宏 → 源码 `#if` 分支」）：

```text
cmake -DMI_SECURE=ON   → 宏 MI_SECURE=4   （常规缓解）
cmake -DMI_SECURE=FULL → 宏 MI_SECURE=5   （再给每个 mimalloc 页尾加 guard page，昂贵）
```

`MI_SECURE` 再级联出四个真正的功能宏：

| 宏 | 打开条件 | 作用 |
| --- | --- | --- |
| `MI_PADDING` | `MI_SECURE>=3` 或 `MI_DEBUG>=1` 或 TRACK | 每块尾部加 8 字节 padding 结构（canary+delta） |
| `MI_PADDING_CHECK_BYTES` | `MI_PADDING` 且（`MI_SECURE>=5` 或 `MI_DEBUG>=1`） | 逐字节校验 padding 填充区 |
| `MI_ENCODE_FREELIST` | `MI_SECURE>=3` 或 `MI_DEBUG>=1` | free list 指针编码 |
| `MI_PAGE_KEY_COUNT` | `MI_SECURE>=4` 或 `MI_PADDING` … → 2，否则 1 | 每页 key 的个数 |

注意一个微妙点：**debug 构建也打开了 `MI_PADDING` 和 `MI_ENCODE_FREELIST`**。所以「检测能力」上 debug ≈ secure 的一个子集，差别在于：debug 是为开发者抓 bug（带断言与逐字节检查），secure 是为生产环境抗攻击（带 guard page、地址随机化、`abort()` 策略），且 secure release 构建里 `MI_DEBUG=0`、断言全部消失。

#### 4.1.3 源码精读

权威的分级注释就在 types.h 开头：

- [include/mimalloc/types.h:57-66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L57-L66)：五个级别各自的含义——1：校验非法指针 free、元数据 guard page、arena 地址随机化、元数据损坏即 abort；2：页内相对分配地址随机化；3：缓冲区溢出检查、double free 检查、free list 编码；4：目前与 3 相同（`MI_SECURE=ON`）；5：每个 mimalloc 页尾加 guard page（`FULL`，昂贵）。
- [include/mimalloc/types.h:94-121](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L94-L121)：上面表格中四个功能宏的原始定义。特别注意 [types.h:112-115](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L112-L115) 已把旧的 `MI_CHECK_DOUBLE_FREE` 注释掉——「double free 现在用 padding 检查」，这是 4.4 节的伏笔。
- [CMakeLists.txt:281-290](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L281-L290)：`FULL` → `MI_SECURE=5`、否则 `MI_SECURE=4` 的翻译处。
- [include/mimalloc/types.h:169-175](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L169-L175)：两条编译期 `#error`——secure 模式**强制**页元数据与用户数据分离（`MI_PAGE_META_IS_SEPARATED`），并禁止把小页元数据前移的 `MI_OPT_FREE_SMALL` 优化。这就是「溢出够不到元数据」的第一道墙：元数据根本不和用户块住在一起。
- [include/mimalloc/types.h:192-196](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L192-L196)：`FULL` 在 16KiB 页的平台（如 Apple ARM64）会把 arena slice 从 64KiB 提到 128KiB，注释写明是为了「不因 16KiB guard page 浪费太多」——guard page 是按 OS 页大小计价的。
- [src/options.c:242-259](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L242-L259)：`MIMALLOC_VERBOSE=1` 时打印的构建配置，包含 `secure level: %d` 和 `free lists: encoded with %d key(s)`，是验证当前二进制安全等级的最快手段。

#### 4.1.4 代码实践

1. **实践目标**：确认你手上的库到底编译进了哪些安全特性。
2. **操作步骤**：
   ```bash
   mkdir -p out/release && cd out/release && cmake ../.. && make -j8 && cd ../..
   mkdir -p out/secure  && cd out/secure  && cmake ../.. -DMI_SECURE=ON && make -j8 && cd ../..
   ```
   写一个只调用 `mi_options_print();`（声明于 `mimalloc.h`，见 [src/options.c:262-264](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L262-L264)）的小程序，分别链接两个库各跑一次；或者直接 `MIMALLOC_VERBOSE=1` 运行任意示例。
3. **需要观察的现象**：release 版输出 `secure level: 0` 且没有 `free lists: encoded` 一行；secure 版输出 `secure level: 4`、`free lists: encoded with 2 key(s)`。
4. **预期结果**：两种构建的配置块逐行 diff，差异行恰好对应 4.1.2 表格中条件含 `MI_SECURE` 的宏。（本机未执行，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`MI_SECURE=ON` 的 **release** 构建（`MI_DEBUG=0`）里，`MI_PADDING_CHECK_BYTES` 是 0 还是 1？溢出 1 字节能被检测到吗？

**答案**：是 0——条件是 `MI_PADDING && (MI_SECURE>=5 || MI_DEBUG>=1)`（[types.h:101-103](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L101-L103)）。此时没有逐字节检查，但溢出只要写穿了 padding 结构的 `canary`/`delta` 字段仍会被 4.4 节的 canary 校验抓住；恰好写进填充区前几字节的非破坏性写入则漏检。要逐字节级检测需 debug 构建或 `MI_SECURE=FULL`。

**练习 2**：为什么 secure 模式要 `#error` 禁止 `MI_PAGE_META_SMALL_IS_ALIGNED`？

**答案**：该优化把页元数据放到小页**内部开头**（u3-l4 讲过 `mi_free_small` 靠向下对齐反查），意味着用户块与元数据同页相邻，向前溢出就能直接改写 `mi_page_t`。secure 的核心承诺是元数据不可达，故直接编译期拒绝（[types.h:173-175](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L173-L175)）。

### 4.2 随机之源：ChaCha20 与 theap 级 PRNG（src/random.c）

#### 4.2.1 概念说明

一切加密和随机化都依赖「攻击者猜不到的随机数」。mimalloc 没有用 `rand()`，而是自带一个 ChaCha20 密码学 PRNG，理由写在文件头注释里： predictable 的性能、避免 libc 内部锁；只需要 OS 随机源做一次种子。注意与 u8-l1 的呼应：分配热路径上**不**调用这里（太贵），这里只服务于 key 生成与初始化期随机化。

#### 4.2.2 核心流程

```text
OS 随机源 (getrandom 等, _mi_prim_random_buf)
   │  失败则降级：时钟 + ASLR 地址的弱随机 (_mi_os_random_weak) 并置 weak 标志
   ▼
_mi_random_init(ctx)  ──► 主 theap 的 random
   │  每新建一个 theap：_mi_random_split(旧ctx, 新ctx)
   ▼                                        （ nonce = 新ctx地址 ^ 随机数，保证不重用 ）
各 theap 独立的 ChaCha20 流
   ├── page->keys[0], page->keys[1]        （每页两个 key，4.3 节）
   ├── padding canary 的密钥素材           （4.4 节）
   ├── theap->cookie                       （奇数哨兵值）
   └── free list 随机顺序 / 地址随机化       （4.5 节）
```

ChaCha20 的输入矩阵是 16 个 32 位字：0..3 常量、4..11 密钥、12..13 计数器、14..15 nonce。每输出 16 个字就把计数器加一，借位会一路推进到 nonce。

#### 4.2.3 源码精读

- [src/random.c:35-40](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c#L35-L40)：`qround`——ChaCha 的四分之一轮，纯 C 的模加、异或与 32 位循环左移，注释说明现代编译器能把它编得很好（x64 无寄存器溢出、clang 用 SSE）。
- [src/random.c:42-74](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c#L42-L74)：`chacha_block`——20 轮混淆后与初始态相加得到输出块，并推进 64 位计数器（借位时继续推进 nonce）。
- [src/random.c:142-155](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c#L142-L155)：`_mi_random_next`——拼两个 32 位输出为一个 `uintptr_t`，**循环直到非 0**（0 被保留为特殊值，比如 `_mi_theap_empty` 的判定）。
- [src/random.c:163-198](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c#L163-L198)：`_mi_os_random_weak` 与 `mi_random_init_ex`——拿不到 OS 随机时用「本函数地址（ASLR 使其随机）^ 时钟」做弱种子并置 `ctx->weak=true`，同时打印警告 `unable to use secure randomness`。
- [src/random.c:200-212](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c#L200-L212)：`_mi_random_init` / `_mi_random_reinit_if_weak`——init.c 在初始化后期会调用后者**补种**：启动太早（preload 阶段）只拿到弱随机的 ctx，等 OS 随机可用后重播种。
- [src/theap.c:274-290](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L274-L290)：每个 theap 的 PRNG 来源——线程的第一个 theap 用 `_mi_random_init`，后续 theap 用 `_mi_random_split` 从链表头的 ctx 分叉；随后立刻 `theap->cookie = _mi_theap_random_next(theap) | 1`（`|1` 保证非零奇数）。
- [src/theap.c:343-345](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L343-L345)：`_mi_theap_random_next`——全库取随机数的统一入口，读 `theap->random`。

#### 4.2.4 代码实践

1. **实践目标**：肉眼确认「同 size 的连续分配，两次运行的地址布局不同」这一随机化的总效果。
2. **操作步骤**：写程序分配 8 个 48 字节的块，打印每个指针，连续运行 5 次（示例代码）：

   ```c
   /* 示例代码 */
   #include <stdio.h>
   #include <mimalloc.h>
   int main(void) {
     for (int i = 0; i < 8; i++) {
       void* p = mi_malloc(48);
       printf("%p\n", p);
     }
     return 0;
   }
   ```

   分别用 release 与 secure 库编译运行。
3. **需要观察的现象**：两组内块间距稳定（同 size class 定长），但**运行之间**整体基地址在变（OS 级 ASLR）；secure 版额外出现页内顺序差异（4.5 节的随机 free list 所致）。
4. **预期结果**：secure 版相邻两次运行中相同序号块的「页内偏移」不相等的比例明显高于 release 版。（待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_mi_random_next` 拒绝返回 0？

**答案**：0 在 mimalloc 里是保留哨兵——例如 keys 为 0 意味着「未初始化」（[src/page.c:747-750](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L747-L750) 的断言要求 `keys[0] != 0`），`_mi_random_ctx` 也用 `input[0] != 0` 判定已初始化（[src/random.c:129-133](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c#L129-L133)）。

**练习 2**：`_mi_random_split` 用「新 ctx 的地址」当 nonce 的一部分，两个 theap 会不会因此拿到相同随机流？

**答案**：不会。nonce = `新ctx地址 ^ 一次随机数`，且 [src/random.c:120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c#L120) 有断言禁止 nonce 重复；地址本身也因分配位置不同而不同。

### 4.3 free list 指针编码：\(((p \oplus k_2) \lll k_1) + k_1\)（internal.h + page.c）

#### 4.3.1 概念说明

这是 secure 模式最核心的算法。威胁模型：攻击者能溢出写坏空闲块的 `next` 字段。如果 `next` 是明文指针，攻击者直接写入目标地址即可让 `malloc` 吐出任意指针。很多加固分配器用 `p ^ k1`，但源码注释一步步论证了它的弱点，并给出 mimalloc 的双 key 方案。

#### 4.3.2 核心流程

编码（写入 `block->next` 时）：

\[
\text{enc}(p) = \big((p \oplus k_2) \lll k_1\big) + k_1
\]

解码（读出时）：

\[
\text{dec}(x) = \big((x - k_1) \ggg k_1\big) \oplus k_2
\]

其中 \(\lll / \ggg\) 是按 `k1`（一个完整 uintptr 值，旋转量取模字宽）的循环移位。注意旋转量本身就是 key，而不是 0~63 的小常数——这进一步放大了搜索空间。`NULL` 不参与运算，而是用单独的 `null` 哨兵值代替，否则 `(k2<<<k1)+k1` 会作为高频哨兵泄漏信息。

解码后的**健全性检查**：合法的 `next` 必须落在同一页内（或为 NULL），否则判定链表损坏。

#### 4.3.3 源码精读

- [include/mimalloc/internal.h:1173-1196](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1173-L1196)：**必读的设计注释**。为什么 `p^k1` 不够：若攻击者猜到 \(p\)，则 \(p \oplus k_1 \oplus p = k_1\) 直接泄 key；若能读两个块，\((p_1 \oplus k_1) \oplus (p_2 \oplus k_1) = p_1 \oplus p_2\) 消掉 key 泄露指针关系。mimalloc 加 `k2` 后两式分别退化为含旋转的不可结合表达式；「引入左旋是因为 xor 和加法在最低位上是线性的」；两 key **每页独立**，大幅降低 key 复用。
- [include/mimalloc/internal.h:1204-1224](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1204-L1224)：`mi_ptr_decode` / `mi_ptr_encode` 的完整实现，各只有三行核心代码——注意 `#if MI_PAGE_KEY_COUNT==2` 分支，只有一个 key 时 `k2 = mi_rotr(k1,13)` 派生。
- [include/mimalloc/types.h:451-452](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L451-L452)：`mi_page_t` 内的 `uintptr_t keys[MI_PAGE_KEY_COUNT]`，注释标明 `const`——keys 属于页的不可变字段。
- [src/page.c:719-725](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L719-L725)：`_mi_page_init` 里每个新页从 theap PRNG 取两个独立随机数做 keys。**key 的生命周期 = 页的生命周期**。
- [include/mimalloc/internal.h:1245-1267](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1245-L1267)：`mi_block_nextx` / `mi_block_set_nextx`——u5 讲过的本地/跨线程 free 与收割，最终都落到这两个函数；`MI_ENCODE_FREELIST=0` 时它们退化为裸指针赋值，**读写路径形状不变**，这正是「编码是零结构改动的可切换层」的关键。
- [include/mimalloc/internal.h:1271-1293](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1271-L1293)：`mi_block_next` 的损坏检测——解码出的 `next` 若不在本页地址范围内，转调 `_mi_block_next_is_corrupted`。
- [src/options.c:611-614](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L611-L614)：`_mi_block_next_is_corrupted` 打印 `corrupted free list entry ...`（EFAULT）并返回 NULL 终止遍历。

#### 4.3.4 代码实践

1. **实践目标**：亲眼看到「空闲块里的 next 不是指针」。
2. **操作步骤**（示例代码，用 debug 构建最方便，它同样启用编码）：

   ```c
   /* 示例代码：观察编码后的 free list */
   #include <stdio.h>
   #include <mimalloc.h>
   int main(void) {
     void* a = mi_malloc(24);
     void* b = mi_malloc(24);
     printf("a=%p b=%p (b-a=%td)\n", a, b, (char*)b - (char*)a);
     mi_free(a);
     mi_free(b);
     /* a、b 现在都在 free list 里；读它们的头 8 字节 */
     printf("a.next raw = 0x%zx\n", *(size_t*)a);
     printf("b.next raw = 0x%zx\n", *(size_t*)b);
     return 0;
   }
   ```

   用 `out/debug` 下的库编译运行（`MI_DEBUG>=1` ⇒ `MI_ENCODE_FREELIST=1`）。
3. **需要观察的现象**：`b-a` 是 32 或 40 之类的规整块长，但打印出的两个「next 原始值」看起来是完全无规律的比特串，且与 `a`、`b` 的地址毫无对应关系。
4. **预期结果**：原始值不等于任何合法地址（也不互相相等）；用 release 库（未编码）重跑，则链尾块的 next 恰为 0、另一块 next 恰为对方地址。（待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接把 `NULL` 编码成 `enc(NULL)`？

**答案**：链表尾大量出现 NULL，编码值 \(((k_2 \lll k_1)+k_1)\) 会成为高频哨兵，攻击者统计即可恢复它并进一步逼近 keys；所以传入独立 `null` 值（见 [internal.h:1194-1195](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1194-L1195) 注释与 [internal.h:1222](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1222) 的三目运算）。

**练习 2**：`mi_block_next` 的「同页检查」为什么能当损坏检测用？它防的是什么攻击？

**答案**：合法 free list 的 next 只可能指向同页的下一块或 NULL（定长块、侵入式链表永不跨页）。攻击者不知道 keys 时只能写入乱码，解码结果落在页外的概率极高——一次 O(1) 的范围比较就把绝大多数伪造挡下，并顺带抓住意外损坏（[internal.h:1276-1278](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1276-L1278)）。

### 4.4 double-free 与溢出检测：padding canary（alloc.c + free.c）

#### 4.4.1 概念说明

`MI_PADDING` 在每个块尾部放一个 8 字节结构（`canary` + `delta`）。u9-l2 会细讲 padding 的「byte 精确 usable_size」用途；本讲聚焦它的**安全**用法：canary 是用 4.3 的 keys 对块地址编码后的 32 位截断，相当于「这块内存的防伪签名」。free 时重算签名比对——溢出写坏了签名 → 缓冲区溢出；签名变成了墓碑值 → double free。

旧方案（遍历三条链表找该块是否已在链上）因太贵被整体弃用，代码仍保留在 `#if MI_CHECK_DOUBLE_FREE` 后供对照。

#### 4.4.2 核心流程

```text
分配时 (alloc.c):
  padding->canary = mi_ptr_encode_canary(page, block, keys)   ← 签名（低9位清零）
  padding->delta  = 块长 - 用户精确字节数

free 时 (free.c mi_check_padding_on_free):
  重算签名 == padding->canary 且 delta <= 块长 ？
    ├─ 是 → 校验通过；把 padding->canary 改写为墓碑 0x00DEAD00   ← 标记「已释放」
    │        （若开启 MI_PADDING_CHECK_BYTES，再逐字节核对填充区是否仍为 MI_DEBUG_PADDING）
    └─ 否 → canary == 0x00DEAD00 ？
              ├─ 是 → "double free detected ..." (EAGAIN，secure 下不 abort，忽略并返回)
              └─ 否 → "buffer overflow in heap block ..." (EFAULT，secure 下 abort())
```

墓碑 `0x00DEAD00` 的 bit 9 是区分位：合法 canary 生成时被强制清掉低 9 位，所以任何合法签名都不可能等于墓碑值。

#### 4.4.3 源码精读

- [include/mimalloc/types.h:544-550](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L544-L550)：`mi_padding_s`——`canary`（编码后的块值）与 `delta`（padding 字节数）各 4 字节。
- [include/mimalloc/internal.h:1226-1243](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1226-L1243)：`mi_ptr_encode_canary`（截断 32 位并清低位，注释引 issue #951：防「虚假的读溢出」变成安全问题）、`mi_ptr_encode_canary_freed`（墓碑 `0x00DEAD00`，注释「置 bit 9 使之区别于任何合法 canary」）、`mi_ptr_decode_canary_is_freed`。
- [src/alloc.c:104-120](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/alloc.c#L104-L120)：分配侧——算 `delta`、写 canary、可选地用 `MI_DEBUG_PADDING` 字节填充填充区。
- [src/free.c:618-632](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L618-L632)：`mi_page_decode_padding`——校验 `canary 匹配 && delta <= bsize`；失败且要求检测 double-free 时读墓碑，成功时**顺手写墓碑**。一行代码完成「检测 + 标记」。
- [src/free.c:687-711](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L687-L711)：`mi_verify_padding`——canary 校验 + 可选的逐字节填充校验（`MI_PADDING_CHECK_BYTES`），并回报出错的字节偏移。
- [src/free.c:713-733](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L713-L733)：`mi_check_padding_on_free`——把校验失败翻译成两条错误消息：double free（EAGAIN）与 buffer overflow（EFAULT，`write after %zu bytes` 给出精确越界位置）。
- [src/free.c:28-33](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L28-L33) 与 [src/free.c:63-66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L63-L66)：本地与跨线程两条 free 路径的**第一件事**都是 padding 校验——检测点在一切账目变更之前，失败直接 return，块不进链表。
- [src/options.c:566-589](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L566-L589)：`mi_error_default` 的 abort 策略——**secure 下 EFAULT 一律 `abort()`**（注释：corrupted meta-data）；EAGAIN（double free）不 abort，与 readme 的「double free 被检测并忽略」呼应。
- [src/free.c:553-611](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L553-L611)：被弃用的旧 double-free 检查——`mi_list_contains` 沿三条链线性找块（还要防 double free 造成的环），`mi_block_could_be_double_free` 做快速预筛。对照新方案「一次 32 位比较」，就能体会注释「double-free is now checked with padding」的分量。

#### 4.4.4 代码实践

1. **实践目标**：触发并读懂 double free 的错误报告，定位到源码行。
2. **操作步骤**（示例代码）：

   ```c
   /* 示例代码：double free */
   #include <stdio.h>
   #include <mimalloc.h>
   int main(void) {
     void* p = mi_malloc(64);
     mi_free(p);
     mi_free(p);          /* 第二次 free：canary 已是墓碑 */
     printf("still alive\n");
     return 0;
   }
   ```

   分别链接 release 库与 secure 库（`out/secure`，注意库名带 `-secure` 后缀，承接 u1-l2）运行。
3. **需要观察的现象**：release 版无任何输出差异、打印 `still alive`（第二次 free 把块再次头插进 `local_free`，链表出现重复元素，静默埋雷）；secure 版在 stderr 打印 `double free detected of heap block 0x... with size ...`，程序**不** abort，继续打印 `still alive`。
4. **预期结果**：按 [free.c:627-628](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L627-L628)（写墓碑）→ [free.c:722-725](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L722-L725)（比对失败→识别墓碑→EAGAIN 消息）→ [options.c:576-580](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L576-L580)（EAGAIN 不在 abort 列表）逐层对应。（待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么合法 canary 的低 9 位必须是 0？

**答案**：为了给墓碑腾出编码空间。`mi_ptr_encode_canary` 清低位防「虚假读溢出」（issue #951），而墓碑 `0x00DEAD00` 恰好置位 bit 9——两者因此不可能碰撞（[internal.h:1228-1239](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1228-L1239)）。

**练习 2**：把 4.4.4 的例子改成「释放前写 `((char*)p)[mi_usable_size(p)] = 1;`」，secure 版会发生什么？与 double free 的行为差异说明了什么？

**答案**：这一字节写进填充区/canary，`mi_verify_padding` 失败，打印 `buffer overflow in heap block ...`（EFAULT）；**secure 下 EFAULT 会 `abort()`**（[options.c:576-580](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L576-L580)），程序不会打印后续内容。差异说明 mimalloc 把「堆结构被破坏」视为比「重复释放」更严重的事件：前者主动熔断，后者报告后忽略。（行为细节待本地验证。）

**练习 3**：跨线程 free（u5-l2 的 `mi_free_block_mt`）也会做 padding 校验吗？

**答案**：会。[free.c:63-66](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L63-L66) 注释明确「checking padding is safe for mt」——padding 结构在块尾，非属主线程同样可读可判，只是失败后同样直接 return、不进 `xthread_free` 链。

### 4.5 guard page 与地址随机化：让溢出「够不到」元数据（os.c + arena.c + page.c）

#### 4.5.1 概念说明

guard page（守卫页）是一段被 **decommit** 的虚拟地址：CPU 一访问就陷入 SIGSEGV。mimalloc 的实现非常轻——不是 `mmap(PROT_NONE)` 新映射，而是把已保留内存中的某一页 decommit 掉；恢复也只是重新 commit。这组原语在 os.c，用法则在 arena.c：

- **任何 secure 级别（>=1）**：arena 头部元数据区（`mi_arena_t` + 全部位图）末尾跟一个 guard page——这是 readme「所有内部页元数据被 guard page 环绕」的主要落点；大对象单例页（huge / 超对齐）额外多保留一个 OS 页。
- **仅 FULL（>=5）**：每个 mimalloc 页尾部都放 guard page——用户数据与不可访问间隙交错，越界立刻崩，但 VMA 数量暴涨，readme 不推荐常规使用。
- **地址随机化（>=1）**：对齐分配的 hint 起点与 1GiB 巨页的起始地址都掺入 PRNG 随机量，模拟分配器层的小 ASLR。
- **顺序随机化（>=2）**：新页的 free list 不再按地址顺序初始化，而是随机穿线；分配时还以掷硬币决定「扩展还是复用」，挫败依赖可预测分配顺序的攻击。

#### 4.5.2 核心流程

guard page 的生命周期（**set = decommit（变成守卫），reset = commit（撤销守卫）**）：

```text
arena 创建/页初始化
  └─ _mi_os_secure_guard_page_set_at/before(addr)     ← decommit 一页 ⇒ 守卫生效
       （is_pinned 的内存跳过；arena 自带 commit_fun 时走回调）
页被完全释放
  └─ _mi_os_secure_guard_page_reset_before(...)       ← 重新 commit
       原因：这段 slice 之后可能被合并进大跨度分配，中间不能留守卫
```

随机穿线 free list 的算法：把待初始化的块区按 2 的幂切成至多 64 个「slice」，每次用 PRNG 选一个还有存量的 slice 接一块上去，直到连成一条随机顺序的链。

#### 4.5.3 源码精读

- [src/os.c:69-73](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L69-L73)：`_mi_os_guard_page_size`——就是 OS 页大小，且断言不超过 slice 的 1/4（issue #1166）。
- [src/os.c:171-178](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L171-L178)：`_mi_os_secure_guard_page_size`——**编译期开关本体**：`MI_SECURE>0` 返回页大小，否则恒 0（于是所有「+guard」的尺寸计算在非 secure 构建里自动归零，调用方代码完全不用 `#if`）。
- [src/os.c:180-202](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L180-L202)：`_mi_os_secure_guard_page_set_at`——`is_pinned`（巨页等锁页内存）直接跳过；arena 有 `commit_fun` 回调（用户自管内存，见 u6-l3 的 `mi_manage_os_memory_ex`）则走回调，否则 `_mi_os_decommit`；失败打印 `secure level %d, but failed to commit guard page`。
- [src/os.c:204-231](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L204-L231)：`set_before` / `reset_at` / `reset_before`——`_before` 变体只是把地址前移一个 guard 页，语义即「守卫放在目标区域**紧前面**」。
- [src/arena.c:1641-1643](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1641-L1643)：arena 元数据区尺寸 = 对齐后的信息大小 **+ 一个 guard 页**。
- [src/arena.c:1701-1725](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1701-L1725)：arena 初始化——非 eager-commit 时**只 commit 到 guard 之前**（守卫页天然留在 decommit 态）；eager-commit 时显式调用 `_mi_os_secure_guard_page_set_before` 把元数据区末页 decommit 成守卫；随后清零元数据时也特意少清一个 guard 页。
- [src/arena.c:1164-1168](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1164-L1168)：单例页（huge 对象 / 超对齐分配，u4-l3）在 `MI_SECURE>=2` 起就把 slice 数量加上一个 guard 页并做对齐。
- [src/arena.c:963-969](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L963-L969) 与 [src/arena.c:1030-1035](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1030-L1035)：`MI_SECURE>=5` 时常规页的可写区收缩一个 guard 页，并在页就绪后 set 守卫。
- [src/arena.c:1243-1249](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1243-L1249)：页释放时 reset 守卫，注释给出原因：「之后可能在这页上分配大跨度，中间不能夹守卫」。
- [src/os.c:126-158](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L126-L158)：`_mi_os_get_aligned_hint` 的随机化——secure 下超 32GiB 的请求干脆不给 hint（把命中已知地址的概率压到 1/256，见 issue #372）；hint 基址的初始化掺入 22 位随机（0~4TiB 偏移）。
- [src/os.c:737-755](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L737-L755)：1GiB 巨页区起始地址（32TiB 之后）掺入 12 位随机（0~4TiB 偏移）。
- [src/page.c:533-587](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L533-L587)：`mi_page_free_list_extend_secure`——按 2 的幂切 slice、`_mi_random_shuffle` 每机器字重洗一次（性能考虑）、`counts[]` 控制各 slice 存量，最终 `mi_block_set_next`（编码写入！）串成随机链。
- [src/page.c:693-698](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L693-L698)：`mi_page_extend_free` 的分岔——`MI_SECURE<2` 用顺序版，否则用随机版；配套地 [page.c:618-623](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L618-L623) 把最小扩展量抬到 `8*MI_SECURE`（随机化需要更大的块区才有意义）。
- [src/page.c:879-899](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L879-L899)：分配查页时的掷硬币——`MI_SECURE>=2` 时一半概率优先 `mi_page_extend_free`（拿更靠后的新块）而非复用链头，进一步打乱分配顺序。

#### 4.5.4 代码实践

1. **实践目标**：在 FULL 构建下让越界写撞上 guard page，观察「当场崩溃」与 4.4 节「free 时报告」的时机差异。
2. **操作步骤**：
   ```bash
   mkdir -p out/secure-full && cd out/secure-full && cmake ../.. -DMI_SECURE=FULL && make -j8 && cd ../..
   ```
   用 4.2.4 的示例程序链接该库（注意库名后缀），把循环体改成分配后立刻 `((char*)p)[48] = 1;`（对 48 字节请求写越界）。
3. **需要观察的现象**：若该块恰为被守卫采样的块（FULL 下每页尾部守卫与块尾的距离取决于 size class），程序在**写入当场**收到 SIGSEGV，而不是等到 free。
4. **预期结果**：至少一部分越界写直接段错误；配合 `MIMALLOC_VERBOSE=1` 可确认 `secure level: 5`。（FULL 的守卫位于页尾而非每块尾，能否命中取决于块在页内的位置；如需每块级守卫请看 u9-l2 的 `MI_GUARDED` 采样。待本地验证。）

#### 4.5.5 小练习与答案

**练习 1**：为什么 guard page 用「decommit 已保留内存」而不是重新 `mmap(PROT_NONE)`？

**答案**：arena 内存本来就已整段保留（u6-l3），decommit/commit 只动物理页映射、不动 VMA 布局，开销极小且完全可逆；`_before/_at` 变体只靠地址加减就能把守卫放到目标任意一侧（[os.c:204-231](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/os.c#L204-L231)）。FULL 模式的代价正是反例：每页一个守卫使 VMA 数量激增，readme 警告 Linux 上可能撞到 VMA 上限（[readme.md:422-424](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/readme.md#L422-L424)）。

**练习 2**：`_mi_os_secure_guard_page_size()` 在非 secure 构建返回 0，这个设计为什么优雅？

**答案**：所有调用方（如 [arena.c:1642](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L1642) 的 `info_size = ... + _mi_os_secure_guard_page_size()`）的算式无需 `#if MI_SECURE` 包裹：secure 时多出一页守卫，非 secure 时加 0、行为与普通构建完全一致——用一个返回 0 的函数替代散落各处的条件编译。

**练习 3**：随机穿线 free list 为什么「每 `MI_INTPTR_SIZE` 轮才重洗一次随机数」？

**答案**：`_mi_random_shuffle` 相对昂贵，而一个 64 位洗牌结果里藏着 8 个可分别使用的字节级随机量（`rnd >> 8*round`），摊薄成本——这是 [page.c:566-572](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L566-L572) 注释明确的性能权衡，与 mimalloc 一贯的「热路径抠到字节」风格一致。

## 5. 综合实践：release 与 secure 的正面对比实验

把本讲全部内容串成一次可复现实验。参考官方的故意犯错样本 [test/test-wrong.c:60-124](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c#L60-L124)（它覆盖非法读写、double free、use-after-free、缓冲区上下溢）。

1. 准备三种构建：`out/release`、`out/secure`（`MI_SECURE=ON`）、`out/debug`。
2. 写一个驱动程序（示例代码），依次做四组操作并打印分隔标记：
   - A：分配 64 字节，`mi_usable_size` 之后写 1 字节，然后 `mi_free`；
   - B：分配 32 字节，连续 `mi_free` 两次；
   - C：`mi_free((void*)0x1234567890)`（野指针）；
   - D：只分配不释放（泄漏），程序退出看统计。
3. 对每个构建运行并记录：有无错误消息、消息原文、是否 abort、退出码。
4. 为每条观察到的消息在源码中找到「报告它的那一行」，填入下表（示例答案已给前两行）：

| 观察 | 报告位置 | 类型 |
| --- | --- | --- |
| `double free detected of heap block ...` | [free.c:724](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L724) | EAGAIN，secure 不 abort |
| `buffer overflow in heap block ...` | [free.c:727](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L727) | EFAULT，secure abort |
| 野指针 free 的表现（消息或 SIGSEGV） | 对照 [internal.h:753-768](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L753-L768) 的 `_mi_checked_ptr_page` 与 [internal.h:796-807](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L796-L807) 的分发 | 待填写 |

5. 思考题：为什么 C 组在 debug 构建能拿到 `invalid pointer` 消息（[free.c:186-189](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L186-L189) 的 `#if MI_DEBUG` 分支），而在 secure release 构建更可能是段错误？（提示：默认 secure 构建仍启用 `MI_PAGE_META_IS_ALIGNED` 的算术反查，野指针的向下对齐读会命中未映射区域或 guard page——后者恰好是 4.5 节的功劳。）

预期总结论：release 对 A/B/C 全部沉默（隐患堆积），debug 报告最详尽（含逐字节定位），secure 在「结构被破坏」时最果断（abort），在「重复释放」时报告后忽略。三者的差异完全可以从本讲引用的 `#if` 分支推演出来。全部实验结果待本地验证。

## 6. 本讲小结

- `MI_SECURE` 是 0~5 的分级体系（cmake `ON`→4、`FULL`→5），级联出 `MI_PADDING` / `MI_PADDING_CHECK_BYTES` / `MI_ENCODE_FREELIST` / `MI_PAGE_KEY_COUNT` 四个功能宏；debug 构建会打开其中一个子集。
- 所有随机量源自自带 ChaCha20（[src/random.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/random.c)），按 theap 分叉；拿不到 OS 随机时降级为弱随机并在后期补种。
- free list 指针按 \(((p \oplus k_2) \lll k_1) + k_1\) 双 key 每页独立编码，配合「next 必须同页」的解码后校验，同时防伪造与检测损坏。
- double free 检测已被 padding canary 取代：free 校验签名通过后写墓碑 `0x00DEAD00`，再次 free 比对即中——一次 32 位比较替代旧方案的链表遍历。
- 错误分两档：double free（EAGAIN，报告后忽略）与缓冲区溢出/链表损坏（EFAULT，secure 下 `abort()`）。
- guard page 的本质是「decommit = set、commit = reset」；任何 secure 级别都保护 arena 元数据区，`FULL` 才给每个 mimalloc 页加尾守卫；地址与分配顺序的随机化从 `MI_SECURE>=1/2` 起逐级生效。

## 7. 下一步学习建议

本讲把「故意写坏内存」的检测讲完了，下一讲 **u9-l2（debug 模式、padding 校验与 guarded 采样）** 从开发者视角继续同一条线：`mi_verify_padding` 的逐字节精确定位、`MI_GUARDED` 按采样率给单个对象尾部放 OS guard page（比 FULL 精准且便宜的替代品），以及 debug 断言体系。建议顺带重读 [test/test-wrong.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/test/test-wrong.c) 的注释——它同时是 u9-l5（valgrind/ASAN 工具链）的官方示例。若想深挖编码算法的密码学背景，可延伸阅读 internal.h 注释中引用的 Salsa20/ChaCha20 资料。
