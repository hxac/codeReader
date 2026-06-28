# 压缩编解码器

## 1. 本讲目标

本讲是「流、过滤与压缩」单元的第二讲，承接 [u8-l1 流与过滤管线](u8-l1-stream-filter-pipeline.md)。上一讲已经确立了总框架：一个 filter 本身就是一个 `fz_stream`，它的 `next` 回调从下层 `chain` 拉字节、做变换后写进自己的缓冲——层层嵌套成「洋葱」式解码管线。

本讲要做的是**钻进洋葱的每一层**，回答一个具体问题：

> 当某一层 filter 背后是一座真正的第三方解码库（zlib、libjpeg……）时，MuPDF 是如何把那座库的「流式 API」塞进 `fz_stream` 这个统一外壳里的？

读完本讲，你应当能够：

1. 说出 MuPDF 封装任意第三方流式解码库的**统一四步模式**（状态结构体 / 从 chain 喂输入 / 解出字节回填缓冲 / 构造与析构）。
2. 看懂 `filter-flate.c`（zlib inflate）、`filter-dct.c`（libjpeg DCT）、`filter-basic.c`（ASCIIHex / ASCII85 / RunLength）三份实现，并指出它们的共性与差异。
3. 拿到一个 PDF 的 `/Filter` 名字（如 `FlateDecode`、`DCTDecode`、`ASCIIHexDecode`），能从名字一路追到对应的 `fz_open_*` 构造函数与 `next_*` 回调。

> 术语澄清：本讲规格里出现「fz_filter」一词，但 MuPDF 源码里**并不存在**叫 `fz_filter` 的类型。所谓 filter，约定俗成地就是一个「套在另一个 `fz_stream` 之上」的 `fz_stream`。后文一律用 `fz_stream`（filter）来表述。

---

## 2. 前置知识

本讲默认你已经掌握 u8-l1 的三个结论：

- **拉取（pull）模型**：`fz_stream` 靠 `rp`/`wp` 一对指针界定当前可读缓冲窗口，缓冲空了就调 `next` 回调重新填满。
- **filter 即 stream**：filter 的 `next` 回调内部从 `state->chain`（下层 stream）拉字节，做变换后写进自己的缓冲，因此 filter 可层层串联。
- **解码顺序**：PDF 的 `/Filter` 数组 `[/ASCIIHexDecode /FlateDecode]` 表示存储的是 `hex(flate(O))`，解码时最内层（数组下标 0）先作用，即「先 hex、后 flate」。

此外需要一点点背景：

- **zlib**：通用无损压缩库，解压入口是 `inflate`，数据结构是 `z_stream`，靠 `avail_in/next_in` 描述输入、`avail_out/next_out` 描述输出，每调一次 `inflate` 推进一小段。
- **libjpeg**：C 语言 JPEG 解码库。它的设计是「库主动拉数据」+「出错用 `setjmp/longjmp` 跳转」，所以接入时要给它装一个**数据源管理器（source manager）**和一个**错误管理器（error manager）**。
- **ASCII85 / ASCIIHex**：把二进制字节编码成纯 ASCII 文本的可打印编码，分别用于 PostScript/PDF 流的可读传输。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [source/fitz/filter-flate.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c) | Flate(zlib) 解压 filter | 封装第三方库的最小范例 |
| [source/fitz/filter-dct.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c) | DCT(JPEG) 解码 filter | 最复杂的封装：source manager + 错误跳转 + 内存钩子 |
| [source/fitz/filter-basic.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c) | 基础编码/工具 filter | ASCIIHex、ASCII85、RunLength（无第三方库） |
| [include/mupdf/fitz/stream.h](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h) | `fz_stream` 契约 | `next`/`drop` 回调签名、`fz_new_stream` |
| [source/fitz/compressed-buffer.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/compressed-buffer.c) | 格式无关分发器 | `fz_open_image_decomp_stream`：类型→构造函数 |
| [source/pdf/pdf-stream.c](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c) | PDF 专用分发器 | `build_filter`：PDF 滤镜名→构造函数 |

> 按名字定位编解码实现有**两条分发路径**：PDF 层把 PDF 滤镜名（`FlateDecode` 等）翻成 `fz_open_*`；通用层 `fz_open_image_decomp_stream` 把图像压缩类型枚举（`FZ_IMAGE_FLATE` 等）翻成同一个 `fz_open_*`。两条路径最终都汇聚到上面三个 filter 文件——这正是「同一套编解码被 PDF、PNG、TIFF 等多种入口复用」的根本原因，详见 4.1。

---

## 4. 核心概念与源码讲解

### 4.1 统一封装模式：fz_stream 适配契约与名称分发

#### 4.1.1 概念说明

无论是 zlib、libjpeg 还是纯 C 写的 ASCII85，每个 filter 都要回答同样的三个问题：

1. **库要字节时，从哪里拉？** —— 从下层 `chain`（一个 `fz_stream`）拉。
2. **解出来的字节放哪？** —— 放进 filter 自己的本地 `buffer`，再让上层框架的 `stm->rp/stm->wp` 指向它。
3. **库出错时怎么办？** —— 转成 MuPDF 的异常（`fz_throw`）或警告（`fz_warn`），绝不让异常穿过 C 库的边界自由蔓延。

把这三点抽出来，就是 MuPDF 封装**任何**第三方流式解码库的统一模式。本节先把这个「外壳」讲透，后面三节（4.2/4.3/4.4）只是往这个外壳里填不同的库。

#### 4.1.2 核心流程

filter 的运行骨架由 `fz_stream` 的契约决定。一次「上层读一个字节」的完整过程是：

```
调用方 fz_available / fz_read_byte
        │  发现 rp == wp（缓冲空）
        ▼
   调用 stm->next(ctx, stm, max)        ← 这就是 filter 自己写的 next 回调
        │
        │  next 内部：
        │    1) 从 state->chain 拉字节喂给库
        │    2) 库解出字节写进 state->buffer
        │    3) stm->rp = buffer;  stm->wp = buffer + n;  stm->pos += n
        ▼
   return *stm->rp++                    ← 返回第一个字节，其余留给后续读取
```

关键约束（来自回调文档）：

- `next` **只在缓冲为空时被调用**，它要负责把缓冲重新填满。
- 返回值约定：无数据返回 `EOF`（即 -1）；有数据返回 `*stm->rp++`（即第一个字节，同时把读指针推进一格）。
- `pos` 是「累计已读字节数」，每次填缓冲必须自增，否则长度统计会错。

#### 4.1.3 源码精读

先看外壳本身。`fz_stream` 结构体（[include/mupdf/fitz/stream.h:319-333](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L319-L333)）只存「引用计数 + 状态 + 三个回调 + 缓冲窗口」：

```c
struct fz_stream
{
    int refs;
    int error;
    int eof;
    int64_t pos;
    unsigned char *rp, *wp;   // 当前可读窗口 [rp, wp)
    void *state;              // filter 私有状态
    fz_stream_next_fn *next;  // 缓冲空时被调用以重新填充
    fz_stream_drop_fn *drop;  // 析构
    fz_stream_seek_fn *seek;
};
```

`next` 回调的契约（[include/mupdf/fitz/stream.h:281-297](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L281-L297)）白纸黑字写明了「填充缓冲、返回 `*stm->rp++`」的要求。所有 filter 的 `next_*` 函数都是这个签名的实现。

构造入口 `fz_new_stream`（[source/fitz/stream-open.c:55-81](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/stream-open.c#L55-L81)）只是分配结构体、填 `refs=1`、把传入的 `state/next/drop` 挂上去——它对所有 filter 一视同仁，不知道里面装的是 zlib 还是 JPEG。

再看「缓冲空 → 自动重填」的触发点 `fz_available`（[include/mupdf/fitz/stream.h:405-429](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L405-L429)）：当 `rp==wp` 且未到 EOF，它就调 `stm->next(...)`。注意它把 `next` 抛出的非 `TRYLATER` 异常吞掉、记 `error=1` 当作 EOF——所以 filter 在 `next` 里 `fz_throw` 是安全的，不会让单条流损坏拖垮整个进程。

最后看「按名称定位编解码实现」的**两条分发路径**。

PDF 层 `build_filter`（[source/pdf/pdf-stream.c:213-282](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L213-L282)）把 PDF 滤镜名逐一翻成构造函数：

```c
if (pdf_name_eq(ctx, f, PDF_NAME(ASCIIHexDecode)) || ...)
    return fz_open_ahxd(ctx, chain);
else if (pdf_name_eq(ctx, f, PDF_NAME(ASCII85Decode)) || ...)
    return fz_open_a85d(ctx, chain);
// Flate/LZW/Fax/DCT/JBIG2 等"图像压缩"走 build_compression_params
//   → fz_open_image_decomp_stream(...)
else if (pdf_name_eq(ctx, f, PDF_NAME(Crypt)))
    return pdf_open_crypt_with_filter(...);
else
    fz_warn(ctx, "unknown filter name (%s)", ...);  // 未知名字：警告 + 透传
```

通用层 `fz_open_image_decomp_stream`（[source/fitz/compressed-buffer.c:71-163](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/compressed-buffer.c#L71-L163)）则用 `switch(params->type)` 把图像压缩类型枚举翻成同一个 `fz_open_*`，例如：

```c
case FZ_IMAGE_FLATE:
    head = fz_open_flated(ctx, tail, 15);          // ← 4.3 的主角
    if (params->u.flate.predictor > 1)
        head = fz_open_predict(ctx, head = ...);   // 可选外挂 predict filter
    break;
case FZ_IMAGE_JPEG:
    head = fz_open_dctd(ctx, tail, ..., our_l2factor, NULL);  // ← 4.4 的主角
    break;
case FZ_IMAGE_JBIG2:
    head = fz_open_jbig2d(ctx, tail, ...);
    break;
```

这条路径被 PDF、PNG（`load-png.c`）、TIFF（`load-tiff.c`）等所有图像入口共享——一处实现，处处复用。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`next` 只在缓冲空时被调用、调用后返回 `*rp++`」这条契约。

**操作步骤**（源码阅读型）：

1. 打开 [include/mupdf/fitz/stream.h:281-297](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L281-L297)，读 `fz_stream_next_fn` 的文档注释。
2. 打开 `fz_available`（[stream.h:405-429](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L405-L429)），找到「`len==0` 且未 EOF 时调用 `stm->next`」的那几行。
3. 在草稿纸上画出 4.1.2 的调用流程图。

**需要观察的现象**：`fz_available` 先判 `len = wp-rp`，非零就直接返回——也就是说，只要缓冲里还有字节，`next` 永远不会被调用。

**预期结果**：你能用自己的话解释「为什么 filter 的 `next` 实现里不必维护『还剩多少没读』的状态——框架已经替你管好了缓冲窗口的复用」。

#### 4.1.5 小练习与答案

**练习 1**：`fz_new_stream` 的 `drop` 参数文档说「May not throw exceptions」（不得抛异常）。结合 `fz_available` 会吞掉 `next` 的异常这一点，说明为什么 `drop`（即各 filter 的 `close_*`）必须用 `fz_warn` 兜底而不是 `fz_throw`。

**参考答案**：`close_*` 在 `fz_drop_stream` 析构链路里被调用，此时往往已经处于另一个 `fz_catch` 的清理路径，再抛异常会破坏异常栈的成对 unwind；而且资源回收是「尽力而为」，宁可记一条警告也不应让释放过程失败中断。

**练习 2**：PDF 里 `/Filter [/ASCIIHexDecode /FlateDecode]`，问 `build_filter_chain_drop` 先构造哪个 filter？最终数据流是 `flate(hex(O))` 还是 `hex(flate(O))`？

**参考答案**：`build_filter_chain_drop` 从下标 0 开始循环（[pdf-stream.c:309-315](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/pdf/pdf-stream.c#L309-L315)），先建 `ASCIIHexDecode`（内层），再把它整个作为 `chain` 喂给 `FlateDecode`（外层）。所以存储顺序是 `flate(hex(O))`，解码时 hex 先作用、flate 后作用——与 u8-l1 结论一致。

---

### 4.2 基础编码 filter：ASCIIHex / ASCII85 / RunLength

#### 4.2.1 概念说明

`filter-basic.c` 是认识 filter 模式的最佳起点：里面的编码 filter **不依赖任何第三方库**，纯 C 状态机，逻辑短到能一眼看全。它们都遵循 4.1 的契约，只是「变换」那一步换成简单的字节映射。

- **ASCIIHexDecode（ahxd）**：每两个十六进制字符还原成一个字节，`>` 是结束符。把二进制变成可粘贴的十六进制文本的逆操作。
- **ASCII85Decode（a85d）**：每 5 个 base-85 字符还原成 4 个字节。比 hex 更紧凑（膨胀率 5/4≈1.25 倍，而 hex 是 2 倍）。
- **RunLengthDecode（rld）**：简单的行程编码，`run<128` 表示后面跟 `run+1` 个原样字节，`run>128` 表示把下一个字节重复 `257-run` 次。

#### 4.2.2 核心流程

三者都是「逐字节从 chain 读、状态机解码、累积到本地 buffer、满或到 EOD 就返回」的同一套骨架：

```
while (输出缓冲没满 && 未到 EOD):
    c = fz_read_byte(chain)            // 从下层拉一个字符
    根据 c 推进状态机，向 buffer 写 0~N 个字节
设置 stm->rp = buffer, stm->wp = p
若 p == rp: return EOF
否则:       return *stm->rp++
```

ASCII85 的数学基础：4 个字节是一个 32 位整数 \(V\in[0,2^{32}-1]\)，而

\[
85^{5} = 4\,437\,053\,125 > 2^{32} = 4\,294\,967\,296
\]

所以 5 个 base-85 数字足够无歧义地表示任意 4 字节组。解码就是反复做 `word = word*85 + digit`，凑满 5 位再一次性拆成 4 字节。

#### 4.2.3 源码精读

ASCIIHex 的状态结构（[source/fitz/filter-basic.c:408-413](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L408-L413)）：只有 `chain`、`eod` 标志和一个 256 字节输出缓冲。

`next_ahxd`（[source/fitz/filter-basic.c:440-496](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L440-L496)）核心是「两个 hex 拼一个字节」的小状态机：

```c
if (ishex(c)) {
    if (!odd) { a = unhex(c); odd = 1; }   // 第一个 hex：存高位
    else      { b = unhex(c); *p++ = (a<<4)|b; odd = 0; }  // 第二个：拼成字节
}
else if (c == '>') {                          // EOD
    if (odd) *p++ = (a << 4);                 // 奇数个 hex：末字节高位补 0
    state->eod = 1; break;
}
else if (!iswhite(c))
    fz_throw(ctx, FZ_ERROR_FORMAT, "bad data in ahxd: '%c'", c);  // 非法字符直接抛
```

注意它对非法字符的处理：**直接 `fz_throw`**（不像 flate 那样容忍坏数据）。但这个异常会被 `fz_available` 吞掉、记 `error=1`——所以一条坏 hex 流只让这张图片失败，不会崩进程。

ASCII85 的 `next_a85d`（[source/fitz/filter-basic.c:524-625](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L524-L625)）多处理两个特例：`'z'`（仅当 `count==0` 时展开成 4 个零字节）和 `~>` 结束符的「尾部不足 5 位」分支（count=2/3/4 分别产出 1/2/3 字节）。`unhex` 之类的辅助在文件上方（[filter-basic.c:415-438](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L415-L438)）。

构造函数都极简，以 `fz_open_ahxd`（[source/fitz/filter-basic.c:506-513](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L506-L513)）为例，三行：`keep_stream(chain)` → 初始化状态 → `fz_new_stream(state, next_ahxd, close_ahxd)`。

> **顺带看一个工程细节**：RunLength filter 在构造时主动检测「RLE 压缩炸弹」（[source/fitz/filter-basic.c:731-751](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-basic.c#L731-L751)）。它判断 `chain->next == next_rld`（即 RLD 套 RLD，膨胀比可指数爆炸），命中就警告 `RLE bomb defused` 并直接置 `eof`。这是 u8-l1 提到「压缩炸弹防护」在 filter 层的具体落点之一。

#### 4.2.4 代码实践

**实践目标**：用脑内执行验证 ASCIIHex 解码逻辑。

**操作步骤**：

1. 假设输入流内容是（示例数据）`48 65 6C 6C 6F 3E`，即字符 `H e l l o >`（其中 `>` 是 EOD）。
2. 手动模拟 `next_ahxd` 的状态机：第一对 `48`→`0x48='H'`，第二对 `65`→`'e'`……直到读到 `>` 置 `eod`。
3. 写出 `stm->buffer` 的最终字节序列。

**需要观察的现象**：状态机里 `odd` 标志如何交替；读到 `>` 时若 `odd==1`（奇数个 hex），末字节如何用 `(a<<4)` 补出一个高位。

**预期结果**：输出 5 个字节 `48 65 6C 6C 6F`，对应 ASCII 文本 `Hello`。这正是「hex 编码 → 二进制」的还原。

#### 4.2.5 小练习与答案

**练习 1**：ASCII85 里 `'z'` 字符为什么只在 `count==0` 时才合法？

**参考答案**：`'z'` 是「4 个零字节」的缩写，它代表一个完整的 32 位零组，只能出现在两个完整 5 元组的边界上（即 `count==0`，尚未累积任何数字时）。若在累积到一半时出现 `z`，会破坏「5 字符↔4 字节」的对齐，所以代码里写成 `else if (c=='z' && count==0)`。

**练习 2**：`next_ahxd` 遇到非法字符时 `fz_throw`，但调用者并不会崩。请说出吞掉这个异常的代码在哪。

**参考答案**：在 `fz_available`（[stream.h:415-424](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/stream.h#L415-L424)）里：`fz_catch` 块 `fz_rethrow_if(TRYLATER)` 之后，对其它异常 `fz_report_error` + `fz_warn`，置 `stm->error=1` 并当作 EOF 返回。

---

### 4.3 Flate(zlib) filter：第一个第三方库封装

#### 4.3.1 概念说明

`filter-flate.c` 是把第三方库（zlib）塞进 `fz_stream` 外壳的**最小完整范例**。它只有约 120 行，却完整展示了「库的字节输入从 chain 拉、解压输出回填缓冲、错误码翻译、内存走 fz_malloc」四件事。

zlib 的 API 是「双方都带缓冲的推进式」：你给它 `avail_in/next_in`（输入）和 `avail_out/next_out`（输出），调一次 `inflate`，它消费若干输入、产出若干输出，返回 `Z_OK / Z_STREAM_END / Z_DATA_ERROR` 等状态码。我们的任务就是反复「喂一点、收一点」直到把输出缓冲填满。

#### 4.3.2 核心流程

```
next_flated(ctx, stm, max):
    z.next_out = buffer;  z.avail_out = 4096
    while 还有输出配额 (avail_out > 0):
        avail_in = fz_available(chain, 1)   # 从 chain 拉一段输入（直接借用 chain->rp 指针）
        next_in  = chain->rp
        code = inflate(z, Z_SYNC_FLUSH)     # 推进一步
        chain->rp = chain->wp - avail_in    # 按 zlib 实际消费量推进 chain 读指针
        根据 code 决定继续/警告/抛异常
    stm->rp = buffer;  stm->wp = buffer + (4096 - avail_out);  pos += 产出量
    若没产出: eof = 1; return EOF
    否则:     return *stm->rp++
```

一个精妙的细节：**它不把 chain 的字节拷贝进自己的输入缓冲**，而是直接把 `chain->rp` 当成 zlib 的 `next_in` 喂进去（零拷贝），解压完再根据 `avail_in` 的减少量回推 `chain->rp`。这正是「把库的拉模型适配到 fz_stream 拉模型」的高效做法。

#### 4.3.3 源码精读

状态结构（[source/fitz/filter-flate.c:29-34](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L29-L34)）：`chain` + zlib 的 `z_stream z` + 4 KiB 输出缓冲——完全对应 4.1 说的「状态结构体三件套」。

`next_flated`（[source/fitz/filter-flate.c:46-106](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L46-L106)）的主循环与错误码处理：

```c
code = inflate(zp, Z_SYNC_FLUSH);
chain->rp = chain->wp - zp->avail_in;        // 零拷贝回推

if (code == Z_STREAM_END)          break;                        // 正常结束
else if (code == Z_BUF_ERROR)      fz_warn(... "premature end"); // 输入不够，容忍
else if (code == Z_DATA_ERROR && zp->avail_in == 0) fz_warn(... "ignoring"); // 容忍坏尾
else if (code == Z_DATA_ERROR && !strcmp(zp->msg, "incorrect data check")) { ...容忍... }
else if (code != Z_OK)             fz_throw(ctx, FZ_ERROR_LIBRARY, "zlib error: %s", zp->msg);
```

注意 **flate 对数据错误相当宽容**：校验和错、早截断都只是 `fz_warn` 后 `break`（尽可能多地吐出已解出的字节），只有真正的致命错误才 `fz_throw`。这与 4.2 的 ahxd「遇坏字符即抛」形成对比——策略因库而异，取决于「坏数据是否还能产出部分有用结果」。

内存钩子：zlib 的分配被接到 MuPDF 分配器上（[source/fitz/filter-flate.c:36-44](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L36-L44)）：

```c
void *fz_zlib_alloc(void *ctx, unsigned items, unsigned size) {
    return Memento_label(fz_malloc_no_throw(ctx, (size_t)items*size), "zlib_alloc");
}
```

这样 zlib 内部分配的每一块内存都走 `fz_malloc` 并带 Memento 标签，内存泄漏/越界检测与 MuPDF 主程序一体化。

构造与析构对称：`fz_open_flated`（[source/fitz/filter-flate.c:122-145](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L122-L145)）设 `zalloc/zfree/opaque=ctx`、`inflateInit2(&z, window_bits)`、`keep_stream`、`fz_new_stream`；`close_flated`（[filter-flate.c:108-120](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L108-L120)）逆序：`inflateEnd` → `drop_stream(chain)` → `free(state)`。

> `window_bits` 参数：PDF 里 FlateDecode 用 `15`（32 KiB 滑动窗，标准 zlib 头）；传负值表示 raw deflate（无 zlib 头），见 [filter.h:147-155](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/include/mupdf/fitz/filter.h#L147-L155) 文档。`fz_open_image_decomp_stream` 里 [compressed-buffer.c:118](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/compressed-buffer.c#L118) 正是 `fz_open_flated(ctx, tail, 15)`。

#### 4.3.4 代码实践

**实践目标**：确认「flate 对坏数据的容忍」与「zlib 内存走 fz_malloc」两件事。

**操作步骤**（源码阅读型）：

1. 在 [filter-flate.c:75-94](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L75-L94) 数一共有几种 `code` 分支会被 `fz_warn` 而非 `fz_throw`。
2. 在 [filter-flate.c:36-44](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L36-L44) 找到 `state->z.opaque = ctx` 的设置（[第 131 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-flate.c#L131)），解释它如何让 zlib 回调拿到 `fz_context`。

**需要观察的现象**：`fz_zlib_alloc` 的第一参 `ctx` 并非 zlib 自己传的，而是构造时通过 `z.opaque` 塞进去的——这是 C 库常见的「`opaque` 用户指针」钩子手法。

**预期结果**：你能解释为什么用 sanitize/memento 构建时，zlib 的内部分配也会被纳入泄漏报告。

#### 4.3.5 小练习与答案

**练习 1**：`next_flated` 用 `chain->rp` 直接当 `next_in`（零拷贝）。如果 `inflate` 还没把这段输入消费完（`avail_in > 0`），下一次循环再调 `fz_available(chain, 1)` 会发生什么？

**参考答案**：`fz_available` 先算 `len = wp-rp`，因为上一轮我们只把 `chain->rp` 回推到「已消费处」，未消费的字节仍在 `[chain->rp, chain->wp)` 窗口内，`len>0` 直接返回，不会重新调 chain 的 `next`——即未消费的输入被正确保留到下一轮，不会丢失也不会重复读。

**练习 2**：为什么 `close_flated` 里 `inflateEnd` 失败只是 `fz_warn` 而不是 `fz_throw`？

**参考答案**：呼应 4.1.5 练习 1——`drop`/`close` 处于析构链路，不得抛异常；清理失败只记警告，保证后续的 `fz_drop_stream`/`fz_free` 仍能执行，避免泄漏。

---

### 4.4 DCT(JPEG) filter：最复杂的第三方库封装

#### 4.4.1 概念说明

`filter-dct.c` 封装 libjpeg，是三个 filter 里最复杂的：libjpeg 既是「库主动拉数据」（要求宿主提供 source manager），又是「出错用 `setjmp/longjmp` 跳转」（要求宿主提供 error manager），还要管 CMYK 极性、缩放解码、可选的量化表流。但它依然严守 4.1 的统一模式——只是「变换」那一步换成了一整套 libjpeg 的回调装配。

DCT（Discrete Cosine Transform，离散余弦变换）是 JPEG 内部把 8×8 像素块从空间域变到频域的数学核心；但对 filter 层来说，这些都被 libjpeg 藏起来了，filter 只看到「输入 JPEG 字节流 → 输出逐行像素」。

#### 4.4.2 核心流程

libjpeg 是「**库在解码过程中主动向宿主要字节**」的模型。MuPDF 的做法是给 libjpeg 装一个自定义 source manager，让它的「要字节」请求回拉 chain：

```
[next_dctd 第一次调用 → lazy init]
    jpeg_create_decompress(cinfo)
    装配 source manager:  fill_input_buffer = fill_input_buffer_dct  ← 库要数据时回调
    装载 error manager:   error_exit = error_exit_dct               ← 库出错时回调 → fz_throw
    (可选) 先读 jpegtables 的头
    jpeg_read_header / jpeg_start_decompress
    分配一行像素的 scanline 缓冲

[之后每次 next_dctd]
    循环 jpeg_read_scanlines，把整行像素 memcpy 进 stm->buffer
    （CMYK 且需反相时调 invert_cmyk）
    填满 stm->buffer 后返回 *stm->rp++
```

其中 `fill_input_buffer_dct` 是适配的核心：libjpeg 要字节 → 它调 `fz_available(curr_stm, 1)` 从 chain 拉 → 把 `curr_stm->rp` 当作 `next_input_byte` 交回 libjpeg。

#### 4.4.3 源码精读

状态结构 `fz_dctd`（[source/fitz/filter-dct.c:33-53](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L33-L53)）比 flate 大得多：除了 `chain`/`jpegtables`/`curr_stm`，还内嵌了 `jpeg_decompress_struct cinfo`、`jpeg_source_mgr srcmgr`、`jpeg_error_mgr errmgr`、`jmp_buf jb`、scanline 缓冲与读写指针、输出缓冲——libjpeg 需要的一切都在里面。

**(1) 错误管理器：libjpeg 的 longjmp → fz_throw**

libjpeg 出错时调 `error_exit`，MuPDF 把它重定向到 `error_exit_dct`（[source/fitz/filter-dct.c:119-126](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L119-L126)）：

```c
static void error_exit_dct(j_common_ptr cinfo) {
    char msg[JMSG_LENGTH_MAX];
    fz_dctd *state = JZ_DCT_STATE_FROM_CINFO(cinfo);
    cinfo->err->format_message(cinfo, msg);
    fz_throw(state->ctx, FZ_ERROR_LIBRARY, "jpeg error: %s", msg);
}
```

这样 libjpeg 原本会 `longjmp` 出去的致命错误，被翻译成 MuPDF 的 `fz_throw`，与 flate 的处理殊途同归。`output_message` 被设成空函数 `output_message_dct`（[第 128-131 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L128-L131)）以吞掉 libjpeg 的告警打印。

**(2) 数据源管理器：libjpeg 的「拉」← chain 的「拉」**

`fill_input_buffer_dct`（[source/fitz/filter-dct.c:143-178](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L143-L178)）是把两个拉模型对接的关键：

```c
curr_stm->rp = curr_stm->wp;
src->bytes_in_buffer = fz_available(ctx, curr_stm, 1);   // 从 chain 拉
src->next_input_byte = curr_stm->rp;
if (src->bytes_in_buffer == 0) {                          // chain 也空了 → 伪造 EOI
    fz_warn(ctx, "premature end of file in jpeg");
    src->next_input_byte = eoi;  src->bytes_in_buffer = 2; // {0xFF, JPEG_EOI}
}
```

一个反直觉但关键的兜底：当 chain 提前耗尽（截断的 JPEG），它**注入一个假的 EOI 标记**让 libjpeg 优雅停止，而不是任由 libjpeg 报致命错。`skip_input_data_dct`（[第 180-193 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L180-L193)）则负责 libjpeg 的「跳过若干字节」请求。

**(3) 主循环 `next_dctd` 与 CMYK 反相**

`next_dctd`（[source/fitz/filter-dct.c:206-344](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L206-L344)）首次调用时做 lazy init（跳过开头空白、建解压器、读头、处理 `color_transform` 与 Adobe APP marker 的优先级、设 `scale_num/scale_denom` 实现 `l2factor` 降采样、`jpeg_start_decompress`、分配 scanline），随后用 `jpeg_read_scanlines` 一行行产出像素。

CMYK 极性是个历史包袱（[第 195-204 行注释](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L195-L204)与 [invert_cmyk](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L199-L204)）：独立 JPEG 文件（Photoshop 产生）的 CMYK 是反相的，嵌入 PDF 的 CMYK 是正常的，故 `invert_cmyk` 标志控制是否对每个字节做 `255-x`。`color_transform` 的解析见 [第 256-279 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L256-L279)。

**(4) 内存钩子**

与 flate 同理，libjpeg 的分配也通过自定义内存管理器（`fz_dct_mem_init` + `fz_dct_mem_alloc/free`，[filter-dct.c:70-115](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L70-L115)）走 `fz_malloc` 并带 `dct_alloc` 等 Memento 标签。`SHARE_JPEG` 宏（[第 28-31、55-66 行](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L28-L31)）区分「共享系统 libjpeg」与「内置 libjpeg」两种钩子装配方式。

**(5) 构造与析构**

`fz_open_dctd`（[source/fitz/filter-dct.c:379-410](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L379-L410)）：分配状态、`fz_dct_mem_init`、`keep_stream(chain)` 与 `keep_stream(jpegtables)`、装 error manager、`fz_new_stream(state, next_dctd, close_dctd)`。

`close_dctd`（[source/fitz/filter-dct.c:346-377](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L346-L377)）有两个值得学习的细节：

- 用 `jpeg_abort` 而非 `jpeg_finish_decompress`（[第 351-363 行注释](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L351-L363)）：因为我们常常没把整张图读完就关闭，`finish` 会刷一堆警告，`abort` 等价但安静；且 `abort` 自身可能抛错，故再裹一层 `fz_try/fz_catch` 静默。
- 逆序释放：`jpeg_destroy_decompress` → `fz_dct_mem_term` → 回写 `curr_stm->rp`（让 chain 的读位置停在真正消费处）→ `free(scanline)` → `drop_stream(chain/jpegtables)` → `free(state)`。

#### 4.4.4 代码实践

**实践目标**：定位「libjpeg 主动拉数据」与「libjpeg 致命错转异常」这两处适配代码，理解 source manager 的作用。

**操作步骤**（源码阅读型）：

1. 在 [filter-dct.c:143-178](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L143-L178) 找到 `fill_input_buffer_dct`，确认它通过 `fz_available(ctx, curr_stm, 1)` 从 chain 拉字节。
2. 在 [filter-dct.c:230-237](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L230-L237) 看 source manager 的五个回调槽（`init_source/fill_input_buffer/skip_input_data/resync_to_restart/term_source`）如何被填。
3. 在 [filter-dct.c:119-126](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L119-L126) 确认 `error_exit_dct` 把 libjpeg 错误转成 `fz_throw`。

**需要观察的现象**：libjpeg 自己**没有**「从某个 fz_stream 读」的概念；是 MuPDF 通过填这张 source manager 虚表，把 libjpeg 的拉模型「桥接」到了 fz_stream 的拉模型上。

**预期结果**：你能用一句话说清——「source manager 是 libjpeg 与 fz_stream 之间的适配器（adapter）」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `next_dctd` 把 `jpeg_create_decompress` 等 libjpeg 初始化放在「第一次调用」的 `if (!state->init)` 里，而不是放在 `fz_open_dctd` 构造函数里？

**参考答案**：构造函数只分配状态、不触碰库，可以做到「构造不抛异常」（失败仅因 malloc，已由 `fz_malloc_struct` 处理）；而 libjpeg 的 header 读取、内存钩子初始化可能抛异常，放到首次 `next` 调用正好落在调用方的 `fz_try` 范围内，异常能被正常 catch。这种「构造轻、首调重」的 lazy init 在 fitz 里很常见（参见 u4 设备的 close 时机、u9 store 的按需加载）。

**练习 2**：JPEG 滤镜在 PDF 里常用 `color_transform` 参数；Adobe APP marker 也可能携带 `ColorTransform`。两者冲突时以谁为准？见 [filter-dct.c:264-266](https://github.com/ArtifexSoftware/mupdf/blob/39101dd8179599d5b9653e7a33f157c08e5614eb/source/fitz/filter-dct.c#L264-L266)。

**参考答案**：以 Adobe APP marker 为准——代码先按 PDF 参数（或默认值）设 `state->color_transform`，紧接着 `if (cinfo->saw_Adobe_marker) state->color_transform = cinfo->Adobe_transform;` 用流内标记覆盖之。这保证了解码忠实于图像自身声明的色彩变换。

---

## 5. 综合实践

**任务**：对照 Flate 与 DCT 两个 filter，归纳「封装第三方流式解码库」的统一接口模式。这是本讲规格指定的核心实践。

**步骤**：

1. 重读三个 filter 的状态结构体定义，填下表（待本地阅读后补全）：

   | 关注点 | filter-basic.c (ASCIIHex) | filter-flate.c (zlib) | filter-dct.c (libjpeg) |
   |---|---|---|---|
   | 第三方库 | 无 | zlib | libjpeg |
   | 库的输入怎么来 | `fz_read_byte(chain)` | `fz_available(chain,1)` 借 `chain->rp` | source manager 回调 `fz_available(curr_stm,1)` |
   | 库的输出放哪 | `state->buffer` | `state->buffer` (4 KiB) | `state->buffer` + scanline |
   | 库出错怎么办 | `fz_throw` 坏字符 | `fz_warn` 容忍坏尾 / `fz_throw` 致命 | `error_exit_dct` → `fz_throw` |
   | 库内存走哪 | — | `fz_zlib_alloc`→`fz_malloc` | `fz_dct_mem_alloc`→`fz_malloc` |
   | 构造做了什么 | keep+new_stream | keep+inflateInit2+new_stream | keep+mem_init+装err mgr+new_stream |
   | 析构顺序 | drop+free | inflateEnd→drop→free | abort→destroy→free scanline→drop→free |

2. 用一段话（不超过 150 字）总结这个统一模式。建议提纲：
   - **状态三件套**：`fz_stream *chain` + 库上下文 + 输出缓冲；
   - **输入桥接**：库要字节时一律从 `chain` 拉（`fz_available`/`fz_read_byte`），可用零拷贝借用 `chain->rp`；
   - **输出回填**：解出字节写进本地 buffer，令 `stm->rp/wp` 指向它，返回 `*stm->rp++`；
   - **错误/内存统一**：库错误翻译成 `fz_throw/fz_warn`，库内存经钩子走 `fz_malloc`；
   - **构造 keep、析构逆序 drop**，且 `close_*` 不得抛异常。

3. **可选的可运行验证**（若已按 [u1-l2](u1-l2-build-system.md) 执行 `make tools` 得到 `mutool`）：挑一个含 JPEG 图片的 PDF，用 `./build/release/mutool show ...`（或 `mutool extract`）查看某条流的 `/Filter`，确认它形如 `[/DCTDecode]` 或 `[/FlateDecode]`，从而把「PDF 滤镜名 → 本讲讲的构造函数」这条链路在真实文件上走一遍。具体命令与输出**待本地验证**（取决于你手头的样例 PDF）。

> 一句话归纳（答案样本）：**「filter 是 fz_stream，next 从 chain 拉字节喂库、把库的输出回填进自己的缓冲、把库的错误与内存翻译进 MuPDF 的异常与分配器」——flate/dct/basic 三者只是往这个统一外壳里填了不同的库。**

---

## 6. 本讲小结

- **filter 就是 fz_stream**：它套在另一个 `fz_stream`（`chain`）之上，靠 `next` 回调从 chain 拉字节、变换后回填自己的缓冲；契约由 `fz_stream_next_fn`（返回 `*stm->rp++`）和 `fz_new_stream` 固定。
- **按名定位有两条分发路径**：PDF 层 `build_filter` 把 `ASCIIHexDecode/FlateDecode/...` 翻成 `fz_open_*`；通用层 `fz_open_image_decomp_stream` 把 `FZ_IMAGE_FLATE/JPEG/...` 翻成同一批 `fz_open_*`——PDF、PNG、TIFF 共享同一套编解码实现。
- **基础编码 filter（filter-basic.c）**最简单：ASCIIHex/ASCII85/RunLength 都是纯 C 状态机，逐字节读 chain、解码进 buffer、`fz_read_byte`+`fz_available` 喂入；rld 还内置「RLE 炸弹」检测。
- **Flate（filter-flate.c）**是封装第三方库的最小范例：zlib 的 `z_stream` + 零拷贝借用 `chain->rp` 当 `next_in`、`inflate(Z_SYNC_FLUSH)` 推进、坏数据宽容（`fz_warn`）、内存经 `fz_zlib_alloc` 走 `fz_malloc`。
- **DCT（filter-dct.c）**最复杂：给 libjpeg 装自定义 **source manager**（`fill_input_buffer_dct` 从 chain 拉、耗尽时注入假 EOI）和 **error manager**（`error_exit_dct` 把致命错转 `fz_throw`），并处理 CMYK 反相、`color_transform`、lazy init、`jpeg_abort` 安静析构。
- **统一四步模式**：状态三件套（chain + 库上下文 + 缓冲）→ 从 chain 喂输入 → 回填缓冲返回 `*rp++` → keep 构造 / 逆序 drop 析构（close 不抛异常）。

---

## 7. 下一步学习建议

- **横向铺开**：用本讲的「统一四步模式」去读 filter 家族里没细讲的成员——`filter-dct.c` 的同类 `filter-jbig2.c`（JBIG2）、openjpeg 的 JPX、`filter-basic.c` 里的 `fz_open_faxd`（CCITT 传真，参数最多）与 `fz_open_predict.c`（PNG/TIFF 预测器）。它们都套同一个外壳，对照阅读会非常快。
- **纵向深入**：顺着 u8-l1 的流打开路径往下看 `endstream` filter（防 `/Length` 写错）与 `crypt` filter（PDF 解密），补完「磁盘字节 → 解码字节」的全链路。
- **进入下一单元**：编解码属于「读」侧基础设施；下一讲将转向 u9「性能、缓存与并发」，看 `fz_store` 如何缓存这些解码后的对象（字形/图像），以及多线程下 `fz_clone_context` 如何让多个 filter 链共享同一份 store 而各自独立。
