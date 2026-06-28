# 流与过滤管线

## 1. 本讲目标

本讲是「流、过滤与压缩」单元的第一讲。读完后你应该能够：

- 说清 `fz_stream` 是什么样的数据结构、它用什么机制把「按需读字节」抽象成统一接口；
- 解释一个 filter 为什么「本身也是一个 stream」，以及多个 filter 如何层层嵌套成一条解码管线；
- 描述一条 PDF 流对象（`stream ... endstream`）如何被一步步打开成可读的字节流：先限定长度与解密，再按 `/Filter` 数组逐层套 filter；
- 说清 `/Filter` 数组的顺序语义：为什么数组靠前的 filter 反而最先作用在原始存储字节上；
- 用一条 ASCIIHexDecode + FlateDecode 的双重编码流为例，说清「先 hex 解码、再 zlib 解压」的执行路径。

本讲承接 [u7-l2 xref 与间接对象解析](u7-l2-pdf-xref.md)：xref 把对象编号解析到文件偏移，本讲则回答「找到流对象在磁盘上的位置之后，那一段压缩/编码过的字节如何被还原」。

## 2. 前置知识

在进入源码前，先建立几个直觉。

**什么是「流（stream）」？** 流是一种「按需产出字节」的抽象。它不要求把整个文件一次性读进内存，而是「你要多少、我给多少、给完为止」。它特别适合 PDF：一个几百 MB 的 PDF 里，单个图片流可能只有几 KB，你只想在用到它时才去读、去解压。

**拉取模型（pull model）。** MuPDF 的流是「拉取式」的：上层调用者主动去「要」字节，下层被动地「给」。这与「推送式」（生产者主动推送）相反。拉取模型的好处是消费方完全控制节奏，读到哪算哪，不浪费内存。

**装饰器/洋葱模型。** 一个「过滤器（filter）」包在另一个流外面，对读取到的字节做某种变换（解压、解码、去预测……），再向上层返回变换后的字节。filter 自己也是一个流——所以 filter 可以再被另一个 filter 包住，像洋葱一样层层套起来，形成一条「管线（pipeline）」。

**缓冲窗口 `rp`/`wp`。** 每个 stream 内部都维护一小段「当前可读的字节」，用两个指针 `rp`（read pointer，读指针）和 `wp`（write pointer，写指针）界定：只有 `[rp, wp)` 区间内的字节是对外有效的。`rp == wp` 表示「当前这一小段读完了，需要去下层再拉一批」。

**`setjmp`/`longjmp` 异常。** 流的读取可能失败（文件读错、压缩数据损坏），这些错误通过 `fz_try`/`fz_catch` 抛出。参见 [u2-l3 异常处理](u2-l3-exceptions.md)。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [include/mupdf/fitz/stream.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h) | `fz_stream` 结构定义、`next`/`drop`/`seek` 回调类型、`fz_read_byte`/`fz_available` 等内联读取函数 |
| [source/fitz/stream-open.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c) | 流的构造（`fz_new_stream`）、文件流/内存流的实现 |
| [source/fitz/stream-read.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-read.c) | 读取侧：`fz_read`/`fz_seek`/`fz_read_best`（含压缩炸弹检测） |
| [include/mupdf/fitz/filter.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/filter.h) | 各类 filter 的构造函数声明（`fz_open_ahxd`/`fz_open_flated`/`fz_open_predict` 等） |
| [source/fitz/filter-basic.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c) | ASCIIHex/ASCII85/RunLength/RC4/AES 等基础 filter 实现 |
| [source/fitz/filter-flate.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c) | Flate（zlib inflate）filter 实现 |
| [source/fitz/filter-predict.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-predict.c) | 像素预测 filter（PNG/TIFF predictor） |
| [source/fitz/compressed-buffer.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/compressed-buffer.c) | 按 `fz_compression_params` 分发到具体 filter 的统一入口 |
| [source/pdf/pdf-stream.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c) | PDF 专用层：把 `/Filter`/`/DecodeParms` 字典翻译成 filter 链 |

定位口诀：**通用层的流抽象看 `stream.h` + `stream-open.c` + `stream-read.c`；filter 实现看 `filter-*.c`；PDF 如何把字典拼成链看 `pdf-stream.c`。**

## 4. 核心概念与源码讲解

### 4.1 fz_stream 抽象

#### 4.1.1 概念说明

`fz_stream` 是 MuPDF 所有「按需读字节」的统一抽象。无论是从磁盘读文件、从内存读一段缓冲、还是把压缩字节流解压成原始字节，对外都是同一个 `fz_stream *` 类型，提供同一套读取接口。

它的设计思想是**「结构体 + 三个函数指针」的手写多态**（与 [u3-l1](u3-l1-document-abstraction.md) 的 `fz_document`、[u4-l1](u4-l1-device-model.md) 的 `fz_device` 同出一辙）：

- `next`：当缓冲窗口空了，调用它去「拉一批新字节」进来；
- `drop`：流销毁时，释放该流特有的私有状态；
- `seek`：定位到某个偏移（可选，不支持时为 `NULL`）。

不同来源（文件、内存、filter）只需要各自实现这三个回调，套进同一个 `fz_stream` 外壳即可。这就是为什么 filter 能像洋葱一样层层嵌套——**每个 filter 都是一个完整的 `fz_stream`，它的 `next` 回调内部又去读「被它包住的那个下层 stream」**。

#### 4.1.2 核心流程

一个 stream 的运行节奏可以概括为「缓冲窗口驱动」：

```text
上层调用 fz_read_byte(stm)
        │
        ▼
 stm->rp != stm->wp ？── 是 ──▶ 直接返回 *stm->rp++（命中窗口，零成本）
        │ 否（窗口空了）
        ▼
 调用 stm->next(ctx, stm, max)   ← 各流自定义的「拉取」回调
        │
   next 的职责（约定）：
     1. 用 stm->state 找到私有状态
     2. 从下层来源读取一批字节，填进自己的缓冲区
     3. 让 stm->rp 指向缓冲区头、stm->wp 指向缓冲区尾
     4. return *stm->rp++（返回第一个字节，并把 rp 推进一步）
        │
   若再无数据，next 返回 EOF（-1）
```

关键约定（来自头文件文档）：`next` 返回的不是「字节数」，而是「这一批的第一个字节本身」，同时它已经把 `rp`/`wp` 调整好。`max` 只是一个「你大概想要多少」的提示，可以安全忽略。

引用计数：stream 用 `refs` 管理生命周期，`fz_keep_stream` 自增、`fz_drop_stream` 自减，归零时调用 `drop` 回调并释放外壳。这套机制与 [u2-l2](u2-l2-memory-refcount.md) 讲的 `fz_keep_imp`/`fz_drop_imp` 一致。

#### 4.1.3 源码精读

**结构体定义**——一切的基础，注意三个函数指针 `next`/`drop`/`seek` 与缓冲窗口 `rp`/`wp`：

[include/mupdf/fitz/stream.h:319-333](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L319-L333) —— `struct fz_stream`：`refs` 引用计数、`eof`/`error` 状态、`pos` 逻辑位置、`rp`/`wp` 缓冲窗口指针、`state` 私有状态、`next`/`drop`/`seek` 三个回调。

**`next` 回调的类型契约**——它规定了「拉取函数」必须怎么写：

[include/mupdf/fitz/stream.h:282-297](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L282-L297) —— 文档注释明确：用 `stm->state` 找私有状态、填好 `rp`/`wp`、然后 `return *stm->rp++`；无数据返回 `-1`。`drop`/`seek` 的类型见 [stream.h:307](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L307) 与 [stream.h:317](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L317)。

**构造函数**——把状态和三个回调组装成一个 stream：

[source/fitz/stream-open.c:55-88](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c#L55-L88) —— `fz_new_stream`：分配 `fz_stream` 外壳、置 `refs=1`、清空窗口与位读状态、把传入的 `state`/`next`/`drop` 挂上去（`seek` 默认 `NULL`，由调用方按需补）。

**`fz_read_byte`**——最常用的「读一个字节」，体现了窗口驱动的精髓：

[include/mupdf/fitz/stream.h:442-463](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L442-L463) —— 先看窗口 `[rp,wp)` 有没有货，有就直接返回 `*rp++`；没有且未到 `eof`，就调用 `stm->next` 拉一批。注意错误处理：`next` 抛异常时被 `fz_catch` 接住，报告错误后**当作 EOF 处理**并置 `stm->error=1`（容错策略：读出错不致命地中断，而是优雅收尾）。

**一个真实的 `next` 实现**——文件流如何从磁盘拉一批字节：

[source/fitz/stream-open.c:123-138](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c#L123-L138) —— `next_file`：用 `fread` 往一个 4 KB 的栈缓冲 `state->buffer` 里读，把 `stm->rp` 指向缓冲头、`stm->wp` 指向「读到的末尾」、推进 `stm->pos`，最后 `return *stm->rp++`。这就是「缓冲窗口」的真实填充过程。文件流的私有状态结构见 [stream-open.c:112-121](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c#L112-L121)，里面就一个 `FILE *` 和那个 4 KB 缓冲。

**`fz_open_memory`**——把一段内存直接当成流（filter 管线实践里会用到）：

[source/fitz/stream-open.c:373-387](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c#L373-L387) —— 注意它的 `next` 指向 `next_buffer`，而 `next_buffer` 永远返回 `EOF`（[stream-open.c:322-325](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c#L322-L325)）：因为整段数据已经在内存里，构造时直接把 `rp`/`wp` 指向了 `data`/`data+len`，窗口永远不会「需要再拉」，所以 `next` 用不着干活。

#### 4.1.4 代码实践

**实践目标**：亲手验证「缓冲窗口 + `next` 回调」的拉取模型。

**操作步骤**（源码阅读型 + 可选运行）：

1. 打开 [stream-open.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c)，对照 `next_file` 与 `fz_read_byte`，在纸上模拟「读一个 8 字节文件」的过程：第一次 `fz_read_byte` 时窗口为空 → 调 `next_file` 把 8 字节全读进 4 KB 缓冲 → 返回第 1 字节；后 7 次 `fz_read_byte` 全部命中窗口、不再碰磁盘。
2. （可选，需先按 [u1-l2](u1-l2-build-system.md) `make` 编出库）写一个最小 C 程序，用 `fz_open_file` 打开任一小文件，循环 `fz_read_byte` 直到 `EOF`，统计读了多少字节，与文件实际大小对比。

```c
/* 示例代码：读一个文件并统计字节数（仅供说明流程，非项目原有代码） */
fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
fz_stream *stm = fz_open_file(ctx, "Makefile");
int c, n = 0;
while ((c = fz_read_byte(ctx, stm)) != EOF) n++;
printf("bytes=%d\n", n);   /* 应与 Makefile 字节数一致 */
fz_drop_stream(ctx, stm);
fz_drop_context(ctx);
```

**需要观察的现象**：第一次 `fz_read_byte` 才真正触发磁盘 I/O（`next_file`），其后多次读取命中窗口；读到末尾 `next_file` 因 `fread` 返回 0 而返回 `EOF`，`fz_read_byte` 随之返回 `EOF`。

**预期结果**：统计出的字节数等于文件大小。具体数值待本地验证（取决于你打开的文件）。

#### 4.1.5 小练习与答案

**练习 1**：`fz_open_memory` 的 `next` 回调为什么可以「永远返回 EOF」却不会让读取提前结束？

**参考答案**：因为构造时已经把 `rp`/`wp` 直接指向了整段内存数据（`rp=data, wp=data+len`），窗口里一直有货；只有当 `rp` 推进到 `wp` 之后才会调 `next`，而那时数据确实已读完，返回 `EOF` 正确。`next` 只在「窗口耗尽」时才被调用。

**练习 2**：`fz_read_byte` 在 `next` 抛出异常时，为什么选择「当作 EOF」而不是把异常继续往上抛？

**参考答案**：见 [stream.h:450-459](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L450-L459)。它先 `fz_rethrow_if(FZ_ERROR_TRYLATER)`（这种错必须上抛），其余错误报告后置 `error=1` 并返回 `EOF`。这是一种容错策略：单个流读出错时优雅收尾，避免一个坏流直接中断整页渲染。（注意：`FZ_ERROR_SYSTEM` 这类系统级错误在 `fz_read` 这类批量读取里会重新抛出，见 `fz_read_best`。）

---

### 4.2 filter 链式串联

#### 4.2.1 概念说明

**filter（过滤器）** 是一种特殊的 stream：它包住一个「下层 stream（chain）」，对从下层读到的字节做某种变换，再向上层返回变换后的字节。

举个最直观的例子：`ASCIIHexDecode` filter。PDF 里一段用十六进制文本写的字节（如 `48656c6c6f>`）体积是原始数据的两倍。`ASCIIHexDecode` filter 从下层（原始存储）每读两个十六进制字符，就拼出一个真实字节向上层返回。对上层而言，它看到的就像是「正常的二进制字节流」。

关键性质：**filter 本身是一个完整的 `fz_stream`**。这意味着：

- filter 可以再被另一个 filter 包住（filter 的 chain 可以是另一个 filter）；
- 上层调用者根本分不清自己读的是原始文件、内存、还是某个 filter——它们都是 `fz_stream *`。

于是多个 filter 可以像「洋葱」一样层层套起来，形成一条**解码管线**。例如 PDF 里常见的组合：原始存储（被压缩、被 hex 编码）→ `ASCIIHexDecode` → `FlateDecode` → 解压后的原始字节。

#### 4.2.2 核心流程

filter 的 `next` 回调有一个高度统一的骨架：

```text
next_某filter(ctx, stm, max):
    state = stm->state            # 含一个指向下层 stream 的 chain 指针
    p = state->buffer             # 自己的输出缓冲
    while 还没填满 max 且下层没到尾:
        c = fz_read_byte(state->chain)   # ★ 从下层拉字节
        对 c 做本 filter 的变换
        把变换结果写入 *p++
    stm->rp = state->buffer       # 让窗口指向自己的输出
    stm->wp = p
    stm->pos += (p - buffer)
    return *stm->rp++             # 返回第一个变换后的字节
```

也就是说：**filter 的 `next` 通过读自己的 `chain` 来驱动下层，把变换结果放进自己的缓冲，再让窗口指向它。** 上层 `fz_read_byte(filter)` → filter 的 `next` → `fz_read_byte(chain)` → chain 的 `next` → ……一层层拉取，整条管线就被「拉」动了。这就是「拉取模型」在多 filter 下的自然延伸。

**链的构造顺序 vs 数据流方向**（本讲最容易混淆的点）：

- **构造时**：你先有一个最底层的原始流 `raw`，然后用 `fz_open_X(raw)` 得到一个包住 `raw` 的 filter，再用 `fz_open_Y(那个filter)` 包住它……每一步「把上一步的结果当作 chain 传进去」。
- **读取时**：你从「最后构造出来的那个最外层 filter」开始读。数据流方向是「最外层 → …… → 最内层 → 原始存储」。
- 因此：**最先包住原始字节的那层 filter，在读取时最先作用在原始字节上。**

生命周期：filter 的 `drop` 回调在归零时会 `fz_drop_stream(chain)`，即释放它包住的下层。所以只要持有最外层并 drop 它，整条链会自外向内逐层释放（前提是各层引用计数配平，没有额外的 keep）。

#### 4.2.3 源码精读

**ASCIIHexDecode filter 的完整实现**——最简单、最能说明「filter 即 stream」的例子：

[source/fitz/filter-basic.c:440-496](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L440-L496) —— `next_ahxd`：循环 `fz_read_byte(state->chain)` 从下层取字符；若是十六进制字符，每两个拼成一个字节（`*p++ = (a<<4)|b`）；遇到 `>` 表示数据结束（若落单一个字符，左移补零）；空白字符跳过；非法字符抛 `FZ_ERROR_FORMAT`。最后把 `rp`/`wp` 指向自己的 `buffer`。

[source/fitz/filter-basic.c:506-513](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L506-L513) —— `fz_open_ahxd`：分配私有状态 `fz_ahxd`，**`fz_keep_stream(chain)` 持有下层**，再用 `fz_new_stream` 包装成 stream。注意它的 `next`/`drop` 分别是 `next_ahxd`/`close_ahxd`——这就是「filter 即 stream」的落地：filter 没有新类型，它就是挂了特定回调的 `fz_stream`。

**FlateDecode（zlib inflate）filter**——把压缩字节解压：

[source/fitz/filter-flate.c:29-34](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L29-L34) —— 私有状态 `fz_inflate_state`：含 `chain`（下层）、`z`（zlib 的 `z_stream`）、4 KB 输出缓冲。

[source/fitz/filter-flate.c:46-106](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L46-L106) —— `next_flated`：用 `fz_available(chain, 1)` 从下层拿压缩字节喂给 zlib（`zp->next_in = chain->rp`），调用 `inflate(Z_SYNC_FLUSH)` 解压进自己的 4 KB 缓冲，再把 `rp`/`wp` 指向解压结果。注意它对 zlib 各种返回码做了细分处理（`Z_STREAM_END` 正常结束、`Z_BUF_ERROR`/`Z_DATA_ERROR` 警告但继续、其它才抛异常），体现「尽量容错、不轻易崩溃」的工程取向。

[source/fitz/filter-flate.c:122-145](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L122-L145) —— `fz_open_flated`：与 ahxd 同样的套路——keep 下层、`inflateInit2`、`fz_new_stream` 包装。`window_bits` 参数控制解压窗口（通常 15）。

**Predict filter**——解压之后还要「去预测」的额外一层：

[source/fitz/filter-predict.c:246-304](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-predict.c#L246-L304) —— `fz_open_predict`：同样是 keep chain + 包装。它对图像数据做 PNG/TIFF 风格的「反预测」（把每行像素与其预测值的差还原）。它的 `next_predict` 见 [filter-predict.c:185](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-predict.c#L185)。注意：predict 是「套在解压 filter 之外」的一层，读取时先经过 predict、再经过 flate（见 4.3 的组装）。

**filter 构造函数清单**——所有 filter 的统一签名「`(ctx, chain, 各自参数)` 返回新 stream」：

[include/mupdf/fitz/filter.h:86-202](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/filter.h#L86-L202) —— 可以看到 `fz_open_a85d`（ASCII85）、`fz_open_ahxd`（ASCIIHex）、`fz_open_rld`（RunLength）、`fz_open_dctd`（JPEG）、`fz_open_faxd`（传真）、`fz_open_flated`（zlib）、`fz_open_lzwd`（LZW）、`fz_open_predict`（预测）、`fz_open_arc4`/`fz_open_aesd`（解密）等，签名高度一致，第一个参数都是 `chain`。

**PDF 层如何把 `/Filter` 数组拼成链**——本模块的核心枢纽：

[source/pdf/pdf-stream.c:303-320](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L303-L320) —— `build_filter_chain_drop`：**从 `i=0` 到 `n-1` 顺序遍历 `/Filter` 数组**，每轮把「当前 head」当作 chain 传给 `build_filter_drop` 得到新的 head。所以 `fs[0]` 最先包住原始流（最内层），`fs[n-1]` 最后包（最外层）。读取时数据从最外层 `fs[n-1]` 流向最内层 `fs[0]`，再流向原始字节——**即数组靠前的 filter 先作用在原始字节上**。

[source/pdf/pdf-stream.c:213-282](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L213-L282) —— `build_filter`：按名字分发。`ASCIIHexDecode`/`AHx` 直接返回 `fz_open_ahxd`（L257-258），`ASCII85Decode`/`A85` 直接返回 `fz_open_a85d`（L260-261）；而 `FlateDecode`/`LZWDecode`/`DCTDecode` 等「压缩类」则先经 `build_compression_params`（L234）识别成 `FZ_IMAGE_*` 类型，再统一交给 `fz_open_image_decomp_stream` 构造（L242-255）。`Crypt` 走解密分支（L266-276）。

**FlateDecode 的真实落点**——它不在 `build_filter` 里直接调 `fz_open_flated`，而是经分发器：

[source/fitz/compressed-buffer.c:117-128](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/compressed-buffer.c#L117-L128) —— `fz_open_image_decomp_stream` 的 `FZ_IMAGE_FLATE` 分支：`head = fz_open_flated(ctx, tail, 15)`；若 `/DecodeParms` 里 `Predictor > 1`，再在外面套一层 `fz_open_predict`（`head = predict(flated(tail))`）。这正好印证「predict 是套在解压之外的一层」。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：用一条「ASCIIHexDecode + FlateDecode」双重编码的流，亲手验证 filter 管线「先 hex 解码、再 zlib 解压」的执行顺序，并把它与 `pdf-stream.c` 的数组遍历对应起来。

**背景设定**：假设有一段原始内容 `O`。编码时**先 Flate 压缩、再 ASCIIHex 编码**，得到存储字节 `S = hex(flate(O))`（即外层是 hex 文本、内层是 zlib 压缩）。要还原 `O`，必须**先剥 hex 层、再 inflate**。

**操作步骤**：

1. **读源码确认顺序语义**。打开 [pdf-stream.c:303-320](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L303-L320) 的 `build_filter_chain_drop`。确认：数组 `[ASCIIHexDecode, FlateDecode]`（下标 0 是 ASCIIHex）会被遍历成
   - `i=0`：`chain = fz_open_ahxd(raw)` —— ASCIIHex 最先包住原始字节；
   - `i=1`：`chain = fz_open_flated(上面的ahxd)` —— Flate 包在 ASCIIHex 外面。
   - 于是读取路径为 `读 → flated → ahxd → raw`，即**先 hex 解码、再 zlib 解压**，正好还原 `O`。

2. **手工生成一段双重编码数据**（示例代码，非项目原有）。用 Python 把一段简单文本先 deflate 再 hex：

```python
# 示例代码：制造 hex(flate(O)) 形式的双重编码数据
import zlib
original = b"q Q"                       # 一段合法但极简的 PDF 内容流
compressed = zlib.compress(original)     # 第 1 步：Flate 压缩（内层）
hexed = compressed.hex().upper() + ">"   # 第 2 步：ASCIIHex 编码（外层），'>' 为结束符
print(hexed)                             # 把这串字符喂给 ahxd filter
```

   运行后得到一串大写十六进制文本（以 `>` 结尾）。**该 hex 串的具体内容待本地验证**（zlib 输出与版本/级别有关），但形式一定是形如 `789C...>`。

3. **直接用 filter 管线还原**（绕过 PDF，直接验证链式语义）。把上一步得到的 hex 串塞进内存流，按「内层 ahxd、外层 flated」的顺序套：

```c
/* 示例代码：直接串联 filter 管线（非项目原有代码） */
fz_context *ctx = fz_new_context(NULL, NULL, FZ_STORE_DEFAULT);
fz_try(ctx) {
    const char *hex = "789C....>";            /* 第 2 步打印出的串，待本地填入 */
    fz_stream *raw  = fz_open_memory(ctx, (const unsigned char*)hex, strlen(hex));
    fz_stream *h    = fz_open_ahxd(ctx, raw);      /* 先剥 hex 层（内层） */
    fz_stream *f    = fz_open_flated(ctx, h, 15);  /* 再 inflate（外层）  */
    fz_buffer *out  = fz_read_all(ctx, f, 64);     /* 拉取全部解压后的字节 */
    /* out->data 的前 out->len 字节应为 "q Q" */
    fwrite(out->data, 1, out->len, stdout);
    fz_drop_buffer(ctx, out);
    fz_drop_stream(ctx, f);   /* drop 最外层即可触发整链逐层释放 */
}
fz_catch(ctx)
    fz_report_error(ctx);
fz_drop_context(ctx);
```

   注意构造顺序 `raw → ahxd → flated` 与 `build_filter_chain_drop` 的遍历结果完全一致：`ahxd` 对应数组下标 0（最先包 raw），`flated` 对应下标 1（包在最外）。

**需要观察的现象**：

- 第 3 步标准输出应打印 `q Q`（即原始内容被正确还原）。
- 若故意**交换**两层顺序（`flated` 在内、`ahxd` 在外），即让 inflate 先去啃 hex 文本，会失败——因为 hex 文本不是合法的 zlib 流，`next_flated` 会报 zlib 错误。这反向证明：**只有「先 hex 后 flate」的顺序才能正确解码**，与数组 `[ASCIIHexDecode, FlateDecode]` 的语义吻合。

**预期结果**：正确顺序下输出 `q Q`；交换顺序下报 zlib 解压错误。具体报错文案待本地验证。

> 说明：本实践也可以走 PDF 路径——把上述 hex 串写进一个最小 PDF 的内容流对象（`/Filter [/ASCIIHexDecode /FlateDecode]`），用 `mutool` 渲染验证。但直接用 filter API 更简洁、更聚焦于「管线本身」。

#### 4.2.5 小练习与答案

**练习 1**：为什么说「filter 没有引入新的数据类型」？

**参考答案**：因为 filter 就是一个挂了特定 `next`/`drop` 回调的普通 `fz_stream`。从 [filter-basic.c:506-513](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L506-L513) 可见，`fz_open_ahxd` 最终调用的还是 `fz_new_stream`，返回的是 `fz_stream *`。上层无法、也无需区分它读的是文件、内存还是某个 filter。

**练习 2**：PDF 里 `/Filter [/ASCIIHexDecode /FlateDecode]` 与 `/Filter [/FlateDecode /ASCIIHexDecode]` 解码出来的内容一样吗？

**参考答案**：**不一样**，二者对应不同的存储格式。前者存储是 `hex(flate(O))`（先 hex 后 flate 解码）；后者存储是 `flate(hex(O))`（先 flate 后 hex 解码）。`build_filter_chain_drop` 按下标顺序把 `fs[0]` 放最内层，所以数组顺序直接决定哪一层先作用在原始字节上。写错顺序会导致解压/解码失败。

**练习 3**：`fz_open_predict` 在管线里处于什么位置？为什么？

**参考答案**：处于解压 filter 的**外层**（`head = predict(flated(tail))`，见 [compressed-buffer.c:117-128](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/compressed-buffer.c#L117-L128)）。因为预测编码作用于「已解压的像素字节」，所以必须先 inflate、再去预测，predict 自然在读取路径上排在 flate 之后（即构造时套在 flate 外面）。

---

### 4.3 流打开与读取

#### 4.3.1 概念说明

前两模块讲的是「通用层」的 stream 与 filter。本模块回答：**一个 PDF 流对象（`x 0 obj ... stream <字节> endstream`）到底是怎么变成可读的 `fz_stream` 的？**

这里要叠加三层关注点：

1. **限长（length bound）**：流对象的 `/Length` 给出字节长度。但 PDF 文件常被各种工具破坏，`/Length` 可能写错。MuPDF 用 `endstream` filter 既按 `/Length` 读、又随时警惕 `endstream` 标记，宽容处理坏文件。
2. **解密（decryption）**：若整个 PDF 被加密（参见 [u3-l4](u3-l4-password-metadata.md)），每个流在解压/解码**之前**要先解密。解密本身也是一个 filter（`arc4` 或 `aesd`）。
3. **解码（decoding）**：按 `/Filter`/`/DecodeParms` 字典套上解压/解码 filter 链。

读取侧同样有几个工程要点：`fz_read` 的批量读取、`fz_read_best` 的**压缩炸弹检测**（防止一个很小的压缩流解压出上百 MB 撑爆内存）。

#### 4.3.2 核心流程

一个 PDF 流对象 `obj` 的打开过程（见 `pdf_open_filter`）：

```text
pdf_open_filter(doc, stmobj, num, offset):
    filters = stmobj 的 /Filter（名字或数组）
    params  = stmobj 的 /DecodeParms（参数字典或数组）

    # 第 1 步：raw filter —— 限长 + 解密
    rstm = pdf_open_raw_filter(file_stm, doc, stmobj, num, offset)
        ├─ null/endstream filter：限定只读 /Length 字节，并留意 endstream
        └─ 若 doc->crypt：再套一层 pdf_open_crypt（arc4/aesd）

    # 第 2 步：按 /Filter 套解码链
    if filters 是单个名字:
        fstm = build_filter(rstm, filters, params, ...)        # 单层
    elif filters 是非空数组:
        fstm = build_filter_chain(rstm, filters, params, ...)  # 多层链
    else:
        fstm = fz_keep_stream(rstm)                            # 无 filter，原样

    drop(rstm)   # 中间层引用交由链持有
    return fstm  # 调用方拿到的是「最外层」
```

读取侧：

```text
fz_read(stm, buf, len):      # 批量读取
    循环 fz_available(stm) 把 [rp,wp) 的字节 memcpy 进 buf，直到填满或 EOF

fz_read_best(stm, initial, &truncated, worst_case):  # 读全部进 fz_buffer
    边读边增长 buffer
    若已读量 > worst_case → 抛 "compression bomb detected"   # ★ 防炸弹
```

#### 4.3.3 源码精读

**PDF 流打开的统一入口**：

[source/pdf/pdf-stream.c:383-411](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L383-L411) —— `pdf_open_filter`：先 `pdf_dict_geta` 取 `/Filter`（或缩写 `/F`）和 `/DecodeParms`（或 `/DP`）；调 `pdf_open_raw_filter` 得到底层 raw 流；再按 `filters` 是「单名字」「数组」「无」三种情况分别走 `build_filter` / `build_filter_chain` / 原样返回。

**raw filter：限长 + 解密**：

[source/pdf/pdf-stream.c:336-377](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L336-L377) —— `pdf_open_raw_filter`：先解析出对象的 `num`/`gen`（供解密种子用），用 `pdf_stream_length` 取 `/Length`，套上 `fz_open_endstream_filter`（既按长度读、又防 `/Length` 写错，见 [filter-basic.c:189-285](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L189-L285) 里那段「不信 Length、主动找 endstream」的逻辑）；若文档加密且该流未自带 `Crypt` filter，再套 `pdf_open_crypt`。

**面向对象的便捷入口**：

[source/pdf/pdf-stream.c:491-495](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L491-L495) —— `pdf_open_stream_number`：外部代码（如内容流解释器、图片加载器）打开第 `num` 号对象流的入口。它先 `pdf_cache_object`（u7-l2 讲过的懒加载/缓存），校验该对象确实是流，再调 `pdf_open_filter`。

**批量读取 `fz_read`**：

[source/fitz/stream-read.c:29-52](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-read.c#L29-L52) —— 循环用 `fz_available` 取窗口内字节 `memcpy` 进调用方缓冲，推进 `rp`，直到填满 `len` 或无数据。这是对 `fz_read_byte` 的批量优化版。

**压缩炸弹检测 `fz_read_best`**：

[source/fitz/stream-read.c:80-139](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-read.c#L80-L139) —— 把流全部读进 `fz_buffer`，边读边扩容；关键在 L109-110：若累计读到的大小超过 `worst_case`（默认至少 `MIN_BOMB = 100 MiB`，见 L27），就抛 `"compression bomb detected"`。这能挡住「几 KB 的压缩流解压出几个 GB」的恶意 PDF。出错时若调用方传了 `truncated` 标志，则降级为「返回已读部分」而非抛异常（L126-130）。

**定位 `fz_seek` / 位置 `fz_tell`**：

[source/fitz/stream-read.c:167-205](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-read.c#L167-L205) —— `fz_tell` 用 `pos - (wp-rp)` 给出「逻辑已读位置」；`fz_seek` 委托给 `stm->seek`（若支持），不支持后退的流则用「逐字节读着前进」的慢路径兜底（L194-201）。

#### 4.3.4 代码实践

**实践目标**：用 `mutool` 观察一个真实 PDF 流的 `/Filter`，并把它的解码过程与本模块的源码对应。

**操作步骤**：

1. 找一个含压缩内容流的 PDF（绝大多数 PDF 的页面内容流都是 `/FlateDecode`）。
2. 用 `mutool show` 查看某个流对象的字典，确认 `/Filter` 与 `/Length`：

```bash
# 示例命令：查看第 1 号对象的字典（具体对象号因文件而异）
./build/debug/mutool show input.pdf 1
```

3. 在输出里找到 `/Filter /FlateDecode`（或 `/Filter [/ASCII85Decode /FlateDecode]` 之类的数组）与 `/Length`。
4. 回到源码对照：
   - `/Length` 喂给 [pdf-stream.c:364-365](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L364-L365) 的 `endstream` filter；
   - `/Filter` 喂给 [pdf-stream.c:386-397](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L386-L397) 的分发逻辑。

**需要观察的现象**：`mutool show` 打印的对象字典里能看到 `/Filter`、`/Length`，可能还有 `/DecodeParms`（若用了 predictor）。

**预期结果**：能读到 `/Filter` 字段；若文件被加密，还能看到流对象不直接含 `Crypt` 但文档 trailer 有 `/Encrypt`（此时 `pdf_open_raw_filter` 会自动套解密层）。具体输出待本地验证（取决于所用的 PDF）。

> 命令行具体语法以本地 `mutool` 版本为准；若 `show` 子命令的参数有差异，可改用 `mutool clean -d` 等价查看解码后内容。参见 [u1-l4 mutool](u1-l4-mutool-cli.md)。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pdf_open_raw_filter` 要用 `endstream` filter，而不是简单地按 `/Length` 读固定字节数？

**参考答案**：因为现实中的 PDF 文件常被各种工具破坏，`/Length` 可能写错（偏大或偏小）。`endstream` filter（[filter-basic.c:189-285](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L189-L285)）在按 `/Length` 读的同时，会主动在数据里查找 `endstream` 标记来纠偏，遇到不一致时还会 `fz_warn("PDF stream Length incorrect")`。这是 MuPDF「尽量容错、能读就读」哲学的典型体现。

**练习 2**：`fz_read_best` 里的 `worst_case` 默认如何取值？它的作用是什么？

**参考答案**：见 [stream-read.c:92-95](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-read.c#L92-L95)。若调用方未给上界，取 `initial * 200`，且不低于 `MIN_BOMB`（100 MiB）。作用是防压缩炸弹：一旦解压出的数据超过这个上界，立即抛 `"compression bomb detected"`（L109-110），避免恶意小流撑爆内存。

## 5. 综合实践

把本讲三模块串起来，完成一个「手工解码一条 PDF 流」的小任务。

**任务**：选一个真实 PDF 中的某个流对象，画出它从「磁盘字节」到「上层可读字节」的完整 filter 管线图，并标注每一层对应的源码位置。

**建议步骤**：

1. 用 `mutool show` 找到一个带 `/Filter` 的流对象，记下它的 `/Filter`（数组或单名）、`/DecodeParms`、`/Length`，以及文档是否加密（trailer 的 `/Encrypt`）。
2. 自外向内画出管线。例如某加密 PDF 的图片流 `/Filter [/FlateDecode] /DecodeParms << /Predictor 15 /Columns 100 /Colors 3 >>`，其完整链是：

   ```text
   读 ← fz_read_best
        ↑
   predict(flate)        ← /DecodeParms 里 Predictor>1，由 compressed-buffer.c:119-127 套上
        ↑
   flated(tail, 15)      ← build_compression_params 识别为 FZ_IMAGE_FLATE
        ↑
   crypt(arc4/aesd)      ← pdf_open_raw_filter 因 doc->crypt 套上
        ↑
   endstream(file, len)  ← 限长 + 防 Length 写错
        ↑
   file stream           ← next_file 从磁盘读
   ```

3. 对每一层，在源码里找到它的 `next` 回调（如 `next_flated`、`next_ahxd`、`next_predict`），确认「它从 `state->chain` 读、变换后写进自己的 `buffer`」这个统一骨架。
4. （加分）用 4.2.4 的方法，把这条链里「非加密、非 endstream」的核心解码部分，用 `fz_open_*` 在一个独立 C 程序里复现，验证你能手工还原出原始字节。

**验收标准**：管线图能自洽地解释「为什么这一层在内、那一层在外」，且每一层都能对应到具体的源码文件与函数。

## 6. 本讲小结

- `fz_stream` 是「结构体 + `next`/`drop`/`seek` 三个回调」的手写多态，靠 `rp`/`wp` 缓冲窗口驱动拉取式读取；`fz_new_stream` 是统一构造入口，文件流/内存流/filter 都套同一个外壳。
- **filter 本身就是一个 `fz_stream`**：它的 `next` 从 `state->chain`（下层）拉字节、做变换、写进自己的缓冲。因此 filter 可以层层嵌套成「洋葱」管线。
- PDF 用 `build_filter_chain_drop` **从下标 0 顺序**把 `/Filter` 数组逐层套到原始流上：`fs[0]` 最先包住原始字节（最内层），`fs[n-1]` 在最外层；读取时数据从最外层流向最内层——**数组靠前的 filter 先作用在原始字节上**。
- 对 `[/ASCIIHexDecode /FlateDecode]`，存储是 `hex(flate(O))`，解码「先 hex、后 flate」；顺序写反会失败。
- `ASCIIHexDecode`/`ASCII85Decode` 在 `build_filter` 里有直接分支；`FlateDecode`/`LZWDecode` 等压缩类先识别成 `FZ_IMAGE_*`，再经 `fz_open_image_decomp_stream` 统一构造（Flate 还可能外挂一层 `predict`）。
- 一个 PDF 流对象的打开 = `endstream`（限长 + 防 Length 写错）→ 可选解密 `crypt` → `/Filter` 解码链；读取侧的 `fz_read_best` 带 100 MiB 起跳的压缩炸弹检测。

## 7. 下一步学习建议

- **本单元下一讲 [u8-l2 压缩编解码器](u8-l2-compression-codecs.md)**：横向对比 Flate/DCT(JPEG)/JBIG2/JPX/CCITT 等压缩 filter 如何封装各自的第三方库（zlib/libjpeg/openjpeg/jbig2dec），理解「把库的流式 API 适配成 fz_filter」的统一模式。
- **回到 [u7-l4 资源、页面与内容流解释](u7-l4-resources-content-stream.md)**：本讲解出的是「内容流字节」，下一站可以看这些字节如何被词法分析、被 `pdf_run_processor` 解释成一串 device 回调。
- **进阶阅读**：想理解「写回 PDF 时如何重新压缩/编码」，可对照 [u7-l3 pdf-write.c](u7-l3-pdf-lex-parse-write.md)——它正是本讲 filter 的「逆运算」（encode 方向）。
