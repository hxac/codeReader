# 流式过滤：chunked 解码与 gzip 解压（u4-l6）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `StreamFilter` 抽象接口中每个方法的职责，特别是 `transform()` 的**返回值**与 `getBytesProcessed()` 的区别——前者是「写出了多少数据」，后者是「消费了多少输入」，两者不相等正是「解码器」区别于「搬运工」的本质特征。
2. 沿着 13 个状态走通 `ChunkedDecodingStreamFilter` 的逐字节状态机，解释它为什么**天然免疫 TCP 的粘包与半包**：任意字节边界到达都能正确续算。
3. 跟踪过滤链的完整挂接路径：响应头字段 → `HttpResponse` 工厂 → `HttpResponseCommand` 装配 → `DownloadCommand::installStreamFilter` → 读循环中的 `transform()` 调用，并解释 `sinkFilterOnly_` 标志带来的两条不同代码路径。
4. 说明 gzip/deflate 解压过滤器的挂接条件，以及它为什么会导致「禁用分段下载」和「禁用文件预分配」两个副作用。

本讲是 u4-l1（命令链）与 u4-l2（HTTP 报文解析）的直接下游：前两讲把数据送到了 `DownloadCommand` 手里，本讲回答「数据在写盘之前还要经过哪几道加工」。

## 2. 前置知识

### 2.1 粘包与半包：TCP 是字节流，不是消息流

TCP 只保证字节序完整送达，不保证「一次发送对应一次接收」。发送方连发两条消息，接收方可能一次 `read` 全收到（**粘包**）；一条消息也可能被拆成两次 `read` 才收齐（**半包**）。更麻烦的是拆分点完全随机——可能恰好落在协议字段的正中间。任何流式协议解析器都必须回答：**字节流在任意位置被截断时，如何记住「解析到一半」的状态？** 本讲的答案是：把解析进度编码成状态机的当前状态，每次收到新字节就从上次的断点继续。

### 2.2 Transfer-Encoding: chunked（分块传输编码）

HTTP/1.1 中，若服务器在发送前无法得知实体总长度（动态生成的页面、流式数据），就不能用 `Content-Length`，改用 `Transfer-Encoding: chunked`，把实体切成一连串「带十六进制长度前缀的块」，格式为：

```text
<a-十六进制块大小>[;扩展]\r\n
<块数据，恰好这么多字节>\r\n
<a-十六进制块大小>\r\n
...
0\r\n            ← 大小为 0 的终块
[trailer 头行]*  ← 可选的 trailer
\r\n             ← 空行表示结束
```

例如 `5\r\nhello\r\n0\r\n\r\n` 表示实体内容是 `hello`。承接 u4-l2 的结论：`HttpHeaderProcessor` 一旦发现 `Transfer-Encoding`，会删掉 `Content-Length`/`Content-Range`——传输层的包装长度不等于实体长度，混用必然出错。

### 2.3 Content-Encoding: gzip / deflate（内容编码）

`Content-Encoding` 描述的是**实体本身被压缩过**（RFC 9110 中的表示层概念），与传输层的 chunked 正交。两者可以同时出现：实体先 gzip 压缩，压缩结果再被 chunked 切块传输。因此正确的解码顺序是**先解 chunked 外壳，再解 gzip 内核**——这个顺序直接决定了本讲过滤链的组装顺序。

### 2.4 责任链 / 装饰器模式

`StreamFilter` 用「委托链（delegate chain）」组织：每个过滤器持有一个 `delegate_`（下一个过滤器），自己处理完就把数据转发给下游，链尾永远是一个只写盘不做解码的 `SinkStreamFilter`。这是 C++ 里手写的装饰器链，不需要 GoF 教科书里的类图也能看懂：`A.transform()` 内部调用 `delegate->transform()`，一路传到落盘为止。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/StreamFilter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFilter.h) / [src/StreamFilter.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFilter.cc) | 过滤器抽象接口与委托链的安装逻辑（本讲的「接口契约」） |
| [src/ChunkedDecodingStreamFilter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc) | chunked 解码状态机（本讲的主菜） |
| [src/SinkStreamFilter.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SinkStreamFilter.cc) | 链尾的「只写盘」过滤器，是理解整条链的参照物 |
| [src/DownloadCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc) / [.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.h) | 读循环里调用 `transform()` 的地方，过滤器链的持有者 |
| [src/HttpResponse.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc) | 依据响应头字段创建过滤器对象的「工厂」 |
| [src/HttpResponseCommand.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc) | 把工厂产物装配成链并安装到下载命令上 |
| [src/GZipDecodingStreamFilter.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.h) / [.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc) | gzip/deflate 解压过滤器（libz 封装，编译期依赖 `HAVE_ZLIB`） |
| [test/ChunkedDecodingStreamFilterTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ChunkedDecodingStreamFilterTest.cc) | 状态机的单元测试，含大量半包用例，是最好的行为说明书 |

## 4. 核心概念与源码讲解

### 4.1 StreamFilter 接口：可组合的解码链

#### 4.1.1 概念说明

`DownloadCommand` 从 socket 收到的原始字节不能直接写盘：chunked 的长度前缀和 CRLF 不是文件内容，gzip 的压缩流也不是。但「收数据 → 加工 → 写盘」的循环骨架只有一份，不能为每种编码各写一个下载命令。aria2 的解法是把「加工」抽成 `StreamFilter` 接口：下载命令只管把缓冲区整段喂给链头过滤器，过滤器自己决定消费多少、产出多少、剩下什么状态。

接口里最容易被忽视但最关键的设计是**两个字节计数**：

- `transform()` 的返回值：本次**写入 sink（最终目的地）的字节数**；
- `getBytesProcessed()`：本次**从输入缓冲消费掉的字节数**。

对纯搬运工（`SinkStreamFilter`）两者恒等；对解码器（chunked 剥掉协议开销、gzip 改变数据量）两者几乎永远不等。调用方正是靠第二个数来决定「socket 缓冲区可以丢弃前多少字节」。

#### 4.1.2 核心流程

一次下载中过滤器的生命周期：

```text
init()                     ← 重置内部状态（每个过滤器只 init 一次）
   │
   ▼
┌─────────────── DownloadCommand 每轮读循环 ───────────────┐
│ transform(out, segment, inbuf, inlen)                    │
│   ├─ 解码器消化输入，产出解码数据                          │
│   ├─ 产出数据交给 delegate->transform(...) 逐级下游       │
│   │   （链尾 SinkStreamFilter 调 out->writeData 落盘）    │
│   └─ 记录 bytesProcessed_                                │
│ 调用方：drain(getBytesProcessed()) 丢弃已消费的输入        │
│ 查询 finished() 判断流是否走到终点                         │
└──────────────────────────────────────────────────────────┘
   │
   ▼
release()                  ← 释放资源（如 libz 的 z_stream）
```

接口方法一览：

| 方法 | 职责 |
| --- | --- |
| `init()` | 使用前重置状态；之后可重复 `transform()` |
| `transform(out, segment, inbuf, inlen)` | 消费输入、把结果交给下游；返回写入 sink 的字节数 |
| `finished()` | 流是否已完整解码到终点 |
| `release()` | 释放分配的资源（z_stream 等） |
| `getName()` | 返回类名常量，供链上按名字做判断 |
| `getBytesProcessed()` | 上一次 `transform()` 消费的输入字节数 |
| `installDelegate(filter)` | 把 `filter` 挂到自己链尾（见下） |

#### 4.1.3 源码精读

接口定义在 [src/StreamFilter.h:L49-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFilter.h#L49-L81)：`StreamFilter` 是纯虚基类，唯一的数据成员是 `std::unique_ptr<StreamFilter> delegate_`（L51），即链上下一个过滤器；[L62-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFilter.h#L62-L64) 声明 `transform()`，参数 `out` 是落盘目标（`BinaryStream`，实际是 DiskAdaptor）、`segment` 是写入游标（u3-l4 讲过的 Segment）。

挂链逻辑在 [src/StreamFilter.cc:L46-L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamFilter.cc#L46-L55)：

```cpp
bool StreamFilter::installDelegate(std::unique_ptr<StreamFilter> filter)
{
  if (!delegate_) {
    delegate_ = std::move(filter);
    return true;
  }
  else {
    return delegate_->installDelegate(std::move(filter));
  }
}
```

这段代码做什么：若自己还没有下游，就把 `filter` 收为下游；否则**递归**传给现有下游去找链尾。效果是「总是挂在链的最末端」。后面会看到，这个语义正好配合 `DownloadCommand` 「先装 Sink、再套解码器」的安装顺序，拼出 `chunked → gzip → sink` 的正确次序。

作为对照，链尾的 `SinkStreamFilter::transform()`（[src/SinkStreamFilter.cc:L54-L101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SinkStreamFilter.cc#L54-L101)）展示了「无解码」时两个计数的重合：它按 segment 剩余空间裁剪写入量（L60-69），经写盘缓存或直接 [L89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SinkStreamFilter.cc#L89) 的 `out->writeData()` 落盘，更新 piece 哈希（L91-93）与 segment 写入游标（L94），最后 [L99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SinkStreamFilter.cc#L99) `bytesProcessed_ = wlen`——写多少消费多少，恒等。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「返回值 ≠ bytesProcessed」这一解码器本质。

**操作步骤**：

1. 打开 [test/ChunkedDecodingStreamFilterTest.cc:L35-L43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ChunkedDecodingStreamFilterTest.cc#L35-L43) 的 `setUp()`，注意测试手工组装了一条两层链：`ChunkedDecodingStreamFilter(SinkStreamFilter)`。
2. 阅读第一个用例 [test/ChunkedDecodingStreamFilterTest.cc:L56-L68](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ChunkedDecodingStreamFilterTest.cc#L56-L68)：输入 `"a\r\n1234567890\r\n"`（15 字节，`a` 是十六进制的 10）。
3. 若已在 u1-l2 完成构建，运行 `make check` 执行整个 CppUnit 套件（测试二进制按 [test/Makefile.am:L2-L3](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Makefile.am#L2-L3) 叫 `test/aria2c`）。注意 [test/AllTest.cc:L50](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AllTest.cc#L50) 的 `runner.run()` 不接收命令行参数，所以不能按名字单跑某个用例，只能在输出里搜索 `ChunkedDecodingStreamFilterTest`。

**需要观察的现象**：断言 `r == 10`（写入 sink 的字节数）与 `getBytesProcessed() == 15`（消费的输入字节数）同时成立。

**预期结果**：15 字节进、10 字节落盘，差的 5 字节正是 `"a\r\n"` 与 `"\r\n"` 这两段协议开销。若断言不成立则状态机有 bug——这个测试用例就是「两个计数」语义的活文档。（运行结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `transform()` 不直接返回「消费的输入字节数」，而要另设 `getBytesProcessed()`？

**答案**：`transform()` 的返回值语义是「写入了多少数据到 sink」，供上层统计实际下载量（如 PeerStat 测速）；而调用方 drain socket 缓冲需要的是「输入侧消费了多少」。解码器两者不同（chunked 剥开销、gzip 改变体积），一个返回值无法同时表达两个量，所以接口把它们拆开。

**练习 2**：`installDelegate()` 为什么递归？如果改成「总是替换自己的 delegate_」会发生什么？

**答案**：递归保证新过滤器挂到**链尾**而不是插到中间、也不会截断已有下游。装配流程是先有链尾（SinkStreamFilter 在 `DownloadCommand` 构造时安装），再从外向外套解码器（chunked 套在 gzip 外面）；若直接替换，后装的 Sink 会顶掉先装的解码器链，解码永远不发生。

### 4.2 ChunkedDecodingStreamFilter：逐字节推进的状态机

#### 4.2.1 概念说明

chunked 解码要同时对付三件难事：

1. **长度前缀是变长的**：块大小是 1 到 16 个十六进制字符，读到非十六进制字符才知道结束；
2. **半包随时发生**：`"1"` 之后 CRLF 还在路上，或 `"10\r\n1234567890"` 只到了一半；
3. **终块后有可选 trailer**：`0\r\n` 之后可能有任意多行 trailer 头，最后以空行 `\r\n` 收尾。

aria2 的实现没有用任何「先攒够一整块再处理」的缓冲策略，而是把解析进度编码成 13 个枚举状态，每收到一段输入就**逐字节**驱动状态机前进——解析到一半输入用完了？没关系，状态留在成员变量里，下段输入到达后继续。这就是它免疫粘包/半包的全部秘密。

#### 4.2.2 核心流程

状态定义在 [src/ChunkedDecodingStreamFilter.cc:L50-L64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L50-L64)，语义与转移如下（⇒ 表示迁移条件）：

| 状态 | 含义 / 等待什么 | 迁移 |
| --- | --- | --- |
| `PREV_CHUNK_SIZE` | 期望块大小的**第一个**十六进制字符 | hex ⇒ `CHUNK_SIZE`；其他 ⇒ 抛异常 |
| `CHUNK_SIZE` | 继续积累十六进制值 | hex ⇒ 累积；`';'` ⇒ `CHUNK_EXTENSION`；`'\r'` ⇒ `PREV_CHUNK_SIZE_LF` |
| `CHUNK_EXTENSION` | 忽略块扩展（如 `;q=1`） | `'\r'` ⇒ `PREV_CHUNK_SIZE_LF` |
| `PREV_CHUNK_SIZE_LF` | 期望大小行的 `'\n'` | `'\n'` ⇒ 记 `chunkRemaining_=chunkSize_`；大小为 0 ⇒ `PREV_TRAILER`（终块），否则 ⇒ `CHUNK` |
| `CHUNK` | **批量**转发块数据给 delegate | 转发 `min(chunkRemaining_, 剩余输入)` 字节；计数归零 ⇒ `PREV_CHUNK_CR` |
| `PREV_CHUNK_CR` | 期望块尾 `'\r'` | ⇒ `PREV_CHUNK_LF`；其他 ⇒ 抛异常 |
| `PREV_CHUNK_LF` | 期望块尾 `'\n'` | ⇒ 回到 `PREV_CHUNK_SIZE`（下一块） |
| `PREV_TRAILER` | 终块后：看有没有 trailer | `'\r'`（无 trailer）⇒ `PREV_END_LF`；其他 ⇒ `TRAILER` |
| `TRAILER` | 读一行 trailer 头 | `'\r'` ⇒ `PREV_TRAILER_LF` |
| `PREV_TRAILER_LF` | 期望 trailer 行尾 `'\n'` | ⇒ 回 `PREV_TRAILER`（可能还有下一行） |
| `PREV_END_LF` | 期望最终空行的 `'\n'` | ⇒ `CHUNKS_COMPLETE`（终态） |
| `CHUNKS_COMPLETE` | 流已完整解码 | 立即停止消费输入 |
| `PREV_END_CR` | （定义了但 switch 中无对应 case，是残留的死枚举值） | — |

一次 `transform()` 的伪代码：

```text
bytesProcessed_ = 0
for i in [0, inlen):
    c = inbuf[i]
    if state == CHUNK:                      # 唯一的批量路径
        readlen = min(chunkRemaining_, inlen - i)
        outlen += delegate->transform(inbuf+i, readlen)   # 块数据整段下游
        chunkRemaining_ -= readlen
        i += readlen - 1                    # for 循环还会 ++i，所以先减 1
        if chunkRemaining_ == 0: state = PREV_CHUNK_CR
    else:
        按上表逐字节迁移状态，非法输入抛 DlAbortEx
bytesProcessed_ = i                          # 精确汇报消费量
```

块大小的累积是个简单的按位翻倍过程：每读一个十六进制字符 \(c\)，

\[ chunkSize \leftarrow chunkSize \times 16 + \mathrm{hex}(c) \]

溢出防护在 `CHUNK_SIZE` 状态：[src/ChunkedDecodingStreamFilter.cc:L103-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L103-L105) 先检查 `chunkSize_ & 0x7800000000000000LL` 再左移。`0x78...0` 的二进制高位是 `0111 1000`，即 bit 59~62 任一为 1 时按位与非零，等价于 \(chunkSize \ge 2^{59}\)；此时再乘 16 将达到 \(2^{63}\)，超出有符号 64 位上限 \(2^{63}-1\)。所以「太大」的判定精确对应「再移位就要溢出」。

还有两个值得咀嚼的细节：

- **`CHUNK` 是唯一不逐字节的分支**：块数据不需要解析，能拿多少转发多少，避免了对 MB 级块数据做逐字节循环；
- **终态后精确止损**：到达 `CHUNKS_COMPLETE` 后若缓冲区还有字节（keep-alive 连接上可能紧跟着下一个响应的数据），`goto fin` 立刻退出，`bytesProcessed_` 恰好停在流末尾——多余字节留在 socket 接收缓冲里，归下一个请求的处理流程（u4-l2 讲过的 `HttpConnection` 在途请求队列）。

#### 4.2.3 源码精读

先看成员与构造（[src/ChunkedDecodingStreamFilter.h:L42-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.h#L42-L47)）：整个状态机的持久化状态只有四个变量——`state_`（当前状态）、`chunkSize_`（正在积累的块大小）、`chunkRemaining_`（当前块还差多少数据）、`bytesProcessed_`。**半包恢复能力完全由这几个成员变量承担**，没有任何额外缓冲。

主体 `transform()` 在 [src/ChunkedDecodingStreamFilter.cc:L82-L216](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L82-L216)：

- [L92-L100](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L92-L100)（`PREV_CHUNK_SIZE`）：首字符必须是十六进制（判定函数是 [src/util.cc:L241-L244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L241-L244) 的 `isHexDigit`，字符转数值在 [L499](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L499) 的 `hexCharToUInt`），否则抛 `DL_ABORT_EX("Bad chunk size")`。
- [L101-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L101-L118)（`CHUNK_SIZE`）：含上文分析的溢出防护，以及 `;`（扩展）与 `\r`（大小行结束）两个出口。
- [L124-L138](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L124-L138)（`PREV_CHUNK_SIZE_LF`）：吃掉 `\n` 后把 `chunkSize_` 冻结成 `chunkRemaining_`，并在这里分叉：0 进入终块流程（`PREV_TRAILER`），非 0 进入数据搬运（`CHUNK`）。
- [L139-L149](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L139-L149)（`CHUNK`）：核心三行——`readlen` 取块剩余与输入剩余的较小者，整段交给 `getDelegate()->transform()`（L142，即链上的 gzip 或 sink），`i += readlen - 1`（L144）抵消 for 循环自增。
- [L150-L173](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L150-L173)：块尾 CRLF 校验，缺 CR/LF 都抛异常；随后清零块状态回到 `PREV_CHUNK_SIZE`，准备下一块。
- [L174-L205](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L174-L205)：终块之后的 trailer 循环——空行（`\r` 直接出现）说明没有 trailer，跳到 `PREV_END_LF`；否则逐行读完再回来，直到最终 `\n` 置 `CHUNKS_COMPLETE`。
- [L213-L215](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L213-L215)（`fin` 标签）：`bytesProcessed_ += i`，精确汇报。
- [L218-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L218-L221)：`finished()` 要求**自己到终态且 delegate 也 finished**——这是链式过滤器的一致性约定：任何一环没走完，整条链就没走完。

顺带说明：`init()`（[L79](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L79)）与 `release()`（[L223](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L223)）都是空实现——这个过滤器不持有需要初始化/释放的资源，状态初始化全部在构造函数（[L67-L75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L67-L75)）完成。

#### 4.2.4 代码实践

**实践目标**：用单元测试里现成的半包用例，验证「状态可以跨 `transform()` 调用存活」。

**操作步骤**：

1. 阅读 [test/ChunkedDecodingStreamFilterTest.cc:L97-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ChunkedDecodingStreamFilterTest.cc#L97-L106)：只喂 `"1"` 一个字符——块大小的第一位十六进制数字单独到达，断言返回 0（什么都没写盘）且不抛异常。
2. 接着阅读 [L119-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ChunkedDecodingStreamFilterTest.cc#L119-L129)：喂 `"10\r\n1234567890"`——声明 16 字节的块但只到了 10 字节数据（注意此时**连块尾 CRLF 都没到**），断言 10 字节已全部落盘。
3. 再看 [L131-L137](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/ChunkedDecodingStreamFilterTest.cc#L131-L137)：补喂 `"123456\r\n"`，剩下的 6 字节接续写盘。三次调用模拟了一次「三次 read 才收齐一个块」的传输。

**需要观察的现象**：三次 `transform()` 各自的返回值（0、10、6）与落盘内容逐步拼接成完整的 16 字节。

**预期结果**：状态机在调用之间停在 `CHUNK` 状态并保留 `chunkRemaining_ = 6`，第三段输入到达后无缝续写。若想本地复现，构建后运行 `make check` 并在 CppUnit 输出中确认该套件通过。（运行结果待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：服务器发送 `"5\r\nhello"` 后 TCP 断开（后面的 `"\r\n0\r\n\r\n"` 永远没来）。状态机会停在哪个状态？下载会怎么收场？

**答案**：`"hello"` 转发完后 `chunkRemaining_` 归零，状态迁移到 `PREV_CHUNK_CR` 并停在那里等 `'\r'`。`finished()` 返回 false。此时 socket 收到 EOF，`DownloadCommand::executeInternal` 的 [L233-L235](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L233-L235) 判定「流没结束却 EOF」，抛 `EX_GOT_EOF` 重试异常——按 u4-l1 讲过的 `DlRetryEx` 出口走重试/换源流程。

**练习 2**：为什么 `CHUNK` 状态里是 `i += readlen - 1` 而不是 `i += readlen`？

**答案**：for 循环每轮末尾会执行 `++i`。批量消费 `readlen` 字节后，希望下一轮从第 `readlen` 个字节之后开始，即 `i` 共前进 `readlen`；其中一次由 `++i` 完成，所以循环体内只需手动加 `readlen - 1`。

**练习 3**：若服务器把块大小写成 `"g\r\n..."`（非十六进制），异常从哪里抛出？这个异常属于哪一类（可重试还是终止）？

**答案**：`PREV_CHUNK_SIZE` 状态在 [L98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc#L98) 抛出，用的是 `DL_ABORT_EX` 宏（`DlAbortEx` 家族）。按 u4-l1 的三级出口分类，`DlAbortEx` 走 `tryReserved` 换 URI/镜像，属于「跳过当前源」级别，而不是终止整组任务。

### 4.3 过滤链的挂接：从响应头到 DownloadCommand 读循环

#### 4.3.1 概念说明

前两节有了「接口」和「一种解码器」，本节回答三个装配问题：**过滤器对象从哪来**（响应头决定）、**链的顺序谁定**（协议语义决定）、**链在哪里被驱动**（`DownloadCommand` 的读循环）。

链的顺序由分层语义唯一确定：chunked 是传输层外壳，gzip 是实体层压缩，落盘是终点，所以完整链只能是：

```text
socket 字节流 → [ChunkedDecodingStreamFilter] → [GZipDecodingStreamFilter] → SinkStreamFilter → 磁盘
                （Transfer-Encoding 有才装）      （Content-Encoding 有才装）      （永远在链尾）
```

顺序错了（gzip 在 chunked 外面）会把 gzip 字节当 chunked 语法解析，立刻在 `PREV_CHUNK_SIZE` 抛「not hex string」。

还有一个容易忽略的初始事实：`SinkStreamFilter` 不是装配阶段才创建的，`DownloadCommand` **构造时**就先装好了它（[src/DownloadCommand.cc:L101-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L101-L105)，同时置 `sinkFilterOnly_ = true`）。之后如果 `HttpResponseCommand` 判断需要解码，才把解码器**套在外面**、顶替链头。这样即便是最普通的下载（无任何编码），链也退化成「只有 Sink」依然可用——统一的调用路径，不需要特判。

#### 4.3.2 核心流程

从响应到落盘的装配与驱动全链路：

```text
HttpResponseCommand 收到响应头
  │
  ├─ HttpResponse::getTransferEncodingStreamFilter()
  │     Transfer-Encoding: chunked ⇒ new ChunkedDecodingStreamFilter
  │     其他值 ⇒ nullptr
  ├─ HttpResponse::getContentEncodingStreamFilter()
  │     Content-Encoding: gzip|deflate 且编译进 zlib ⇒ new GZipDecodingStreamFilter
  │     其他值 ⇒ nullptr（仅记日志，按原样保存）
  │
  ├─ 装配 helper：teFilter.installDelegate(ceFilter)   ← chunked 在外、gzip 在内
  │
  └─ createHttpDownloadCommand(filter)
        └─ DownloadCommand::installStreamFilter(filter)
              filter.installDelegate(已有的 Sink 链)     ← Sink 挂到链尾
              streamFilter_ = filter                     ← 链头换人
              sinkFilterOnly_ = (链头名以 "SinkStreamFilter" 结尾)

DownloadCommand::executeInternal()（数据泵，每轮）
  ├─ recv() 到 SocketRecvBuffer
  ├─ sinkFilterOnly_?
  │    是：先按 segment 剩余空间算 bufSize，再 transform(buffer, bufSize)
  │    否：transform(整个缓冲)，bufSize = getBytesProcessed()
  ├─ socketRecvBuffer->drain(bufSize)        ← 丢掉已消费的输入
  └─ segmentPartComplete 判定用到 streamFilter_->finished()
```

#### 4.3.3 源码精读

**工厂：响应头 → 过滤器对象。** [src/HttpResponse.cc:L188-L199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L188-L199) 的 `getTransferEncodingStreamFilter()` 检查 `Transfer-Encoding` 是否定义（[L177-L180](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L177-L180) 的 `isTransferEncodingSpecified()`），值为 `chunked` 才返回新过滤器，否则 `nullptr`（注释里的 TODO 说明多 token 的 Transfer-Encoding 尚未支持）。[L211-L222](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L211-L222) 的 `getContentEncodingStreamFilter()` 在 `HAVE_ZLIB` 编译宏下对 `gzip`/`deflate` 返回 `GZipDecodingStreamFilter`。

**装配：两个 helper 的不同失败策略。** [src/HttpResponseCommand.cc:L88-L104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L88-L104) 处理 Transfer-Encoding：工厂返回 `nullptr`（不认识的编码）时**抛** `EX_TRANSFER_ENCODING_NOT_SUPPORTED`——传输编码不解开就无法知道实体边界，没有降级余地。[L106-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L106-L126) 处理 Content-Encoding：不支持时只**记一条日志**并把压缩内容原样保存——压缩不解顶多是文件不可直接用，边界仍在。两个 helper 都调用 `filter->installDelegate(std::move(delegate))` 把传入的内层挂到外层下面，完成「外→内」的套娃。

**两条装配路径。** 走哪条由 [L275-L291](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L275-L291) 决定：总长度未知（chunked 场景）或 `shouldInflateContentEncoding()` 为真（[L336-L349](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L336-L349)：请求声明过接受 gzip **且**响应是 gzip/deflate）时走 `handleOtherEncoding`，把文件长度清零、标记总长未知——因为解压后的长度在下载结束前无法知道，**分段下载自然被禁用**（分段依赖已知总长，见 u3-l4）；否则走 `handleDefaultEncoding`，只装 Transfer-Encoding 过滤器（[L382-L384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L382-L384)），保留分段能力。未知长度分支（[L309-L324](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L309-L324)）与 `handleOtherEncoding`（[L420-L421](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L420-L421)）都会把 Content-Encoding 过滤器作为 `delegate` 传进 Transfer-Encoding 过滤器，两层同时装配。

**chunked 的零长度特判。** [L422-L425](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L422-L425) 用 `getName()` 判断链头是否为 `ChunkedDecodingStreamFilter`，把结果存进 `chunkedUsed`：之后的零长度快捷路径（[L428](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L428)、[L460](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L460)）都以「没用 chunked」为前提——用了 chunked 的响应即使实体长 0，也必须先把 `0\r\n\r\n` 尾巴读完才能算结束。

**预分配的联动。** [L518-L532](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L518-L532) 的 `decideFileAllocation()` 沿链遍历（`for (StreamFilter* f = filter; f; f = f->getDelegate().get())`），发现任何一环是 gzip 过滤器就返回 false，[L550-L553](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L550-L553) 随即关闭本任务的文件预分配——响应头里的长度是**压缩后**的，按它预分配必然出错（预分配机制详见 u5-l4）。

**安装。** [L536-L559](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L536-L559) 的 `createHttpDownloadCommand()` 创建 `HttpDownloadCommand` 并在 [L554](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L554) 调用 `installStreamFilter()`。真正的安装逻辑在 [src/DownloadCommand.cc:L410-L420](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L410-L420)：

```cpp
streamFilter->installDelegate(std::move(streamFilter_));  // 旧链头(Sink)挂到新链尾
streamFilter_ = std::move(streamFilter);                  // 新解码器成为链头
sinkFilterOnly_ = util::endsWith(name, SinkStreamFilter::NAME);
```

这段代码做什么：把构造时安装的 Sink 链整体挂到新解码器下面，再让解码器顶替链头；最后按链头名字重置 `sinkFilterOnly_`——链头还是 Sink（没有任何编码）就保持 true。`DownloadCommand.h` 中 [L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.h#L52) 的 `streamFilter_` 与 [L62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.h#L62) 的 `sinkFilterOnly_` 就是这条链的全部宿主状态。

**驱动：读循环的双路径。** [src/DownloadCommand.cc:L150-L199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L150-L199)。socket 缓冲为空时才 `recv()`（L150-162，注释解释了这是为 HTTP pipelining 保住后续响应的数据）；拿到数据后按 `sinkFilterOnly_` 分流：

- **true（纯搬运）**：[L165-L184](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L165-L184) **先**按 segment 剩余空间/文件末偏移算出本次最多能收多少（`bufSize`），**再**把这个量喂给 Sink。输入即输出，所以可以先算后喂，防止把 segment 装不下的大量数据硬塞给写入器。
- **false（有解码器）**：[L186-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L186-L195) 把**整个**缓冲交给链头，然后用 `streamFilter_->getBytesProcessed()` 反推 `bufSize`。方向反过来了：解码器消费多少才算多少。此处源码注释特意说明 chunked 的 `0\r\n` 尾巴可能在 segment 写满之后才到——这就是 `segmentPartComplete` 判定离不开 `finished()` 的原因。

两条路径汇合于 [L196](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L196) 的 `drain(bufSize)`（`SocketRecvBuffer` 的接口见 [src/SocketRecvBuffer.h:L61-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/SocketRecvBuffer.h#L61-L69)），随后更新测速（L197-198）。完成判定在 [L212-L231](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L212-L231)：非 Sink 链在「写入位置到达请求末端 **且** `streamFilter_->finished()`」（L215）或单纯 `finished()`（L228）时才算这一段收尾——`finished()` 是链式的（见 4.2.3），保证 chunked 尾巴和 gzip 的 `Z_STREAM_END` 都到齐。

#### 4.3.4 代码实践

**实践目标**：让链的形状「显形」——通过注入一行日志观察不同响应组装出的过滤器链差异。

**操作步骤**：

1. 在 [src/DownloadCommand.cc:L418](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L418) 之后添加一行日志（示例代码，实验后请还原）：

   ```cpp
   A2_LOG_INFO(fmt("filter chain head=%s -> %s", streamFilter_->getName().c_str(),
                   streamFilter_->getDelegate() ? streamFilter_->getDelegate()->getName().c_str()
                                                : "(none)"));
   ```

2. 重新编译（`make -j$(nproc)`，假设已按 u1-l2 配置好构建）。
3. 准备两类下载源：一个普通静态文件（无编码），以及本讲综合实践里的 chunked/gzip 服务器。
4. 分别用 `./src/aria2c -l=log.txt --log-level=info <URL>` 下载，在日志中检索 `filter chain head`。

**需要观察的现象**：普通下载的日志应显示链头为 `SinkStreamFilter`（无 delegate 或 delegate 为空时的 `(none)` 分支）；chunked/gzip 下载显示链头为 `ChunkedDecodingStreamFilter` 或 `GZipDecodingStreamFilter`，delegate 指向下游。

**预期结果**：`sinkFilterOnly_` 的取值与日志中链头名字一一对应，验证 4.3.3 分析的安装逻辑。（编译运行结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 Transfer-Encoding 不支持时抛异常，而 Content-Encoding 不支持时只记日志继续下载？

**答案**：Transfer-Encoding 描述字节流如何被切分包装，不解开就无法确定实体边界——多收的字节不属于本文件，少收则文件残缺，语义上无降级方案。Content-Encoding 只描述实体内容被压缩，边界仍由 Content-Length/chunked 正确给出；不解压最坏是「保存了压缩文件」，下载本身是完整、可校验的，所以可以安全降级为原样保存。

**练习 2**：`installStreamFilter()` 里为什么用 `util::endsWith(name, SinkStreamFilter::NAME)` 而不是 `name == SinkStreamFilter::NAME`？

**答案**：功能上两者在当前代码里等价（链头要么恰好是 `SinkStreamFilter`，要么是某个解码器）。用 `endsWith` 是宽松匹配，哪怕将来出现名字带该后缀的 Sink 变体也不会误判为「有解码器」。它体现的判定意图是「链头是落盘型过滤器 ⇒ 纯搬运模式」。

**练习 3**：带上 `Content-Encoding: gzip` 的响应为什么不能分段下载？结合 `shouldInflateContentEncoding()` 的注释说明。

**答案**：分段下载依赖「已知总长 + 按 Range 切段」，每个连接写各自的偏移区间。但 gzip 流是**整体压缩**的，解压后的字节偏移与请求的 Range 偏移没有任何对应关系，第 2 段的解压数据无法知道该写到文件的哪里。[L339-L341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L339-L341) 的注释原文就是 "on the fly inflation cannot be made with segment download"。所以一旦决定解压，[L278-L287](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L278-L287) 就把长度清零、走单连接流式路径。

### 4.4 GZipDecodingStreamFilter：libz 的流式封装

#### 4.4.1 概念说明

gzip/deflate 解压不需要手写状态机——libz 的 `inflate()` 本身就是一个可增量驱动的流式解码器（内部状态放在 `z_stream` 里，正是 4.2 节「半包状态外置」思想的库版实现）。这个过滤器做的事主要是：初始化 `z_stream`、每轮把输入喂给 `inflate`、把产出的解压数据分批转发给下游、用 `Z_STREAM_END` 判定流结束。头文件注释（[src/GZipDecodingStreamFilter.h:L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.h#L45)）点明它同时能解 gzip 与 deflate 两种格式。

#### 4.4.2 核心流程

```text
init():  inflateInit2(strm_, 47)
         47 = 32 + 15：窗口 15 位 +「自动探测 gzip/zlib 头」标志位
transform(inbuf, inlen):
    挂上输入指针
    loop:
        inflate() 到 16KiB 输出缓冲
        Z_STREAM_END ⇒ finished_ = true（流解到头）
        其他错误码 ⇒ 抛 DlAbortEx
        产出量交给 delegate->transform()
        输出缓冲没填满 ⇒ break（libz 已把现有输入吃干净）
    bytesProcessed_ = inlen - strm_->avail_in   （libz 没吃完的留在 avail_in）
```

注意它和 chunked 过滤器的两点对称性：状态外置（`z_stream` + `finished_` 成员）带来同样的半包免疫；`finished()` 同样要求 delegate 一并完成（[L123-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L123-L126)）。

#### 4.4.3 源码精读

- [src/GZipDecodingStreamFilter.cc:L57-L72](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L57-L72)（`init()`）：先 `release()` 旧的 `z_stream` 再新建，[L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L69) 的 `inflateInit2(strm_, 47)` 是关键——第二个参数 47 编码了「15 位窗口 + 32 的格式自动探测」，使同一个流既能解 gzip（带 magic 头）也能解 zlib/deflate 包装，对应 `Content-Encoding` 的两种取值。失败则抛 `DlAbortEx`。与之配对的 `release()`（[L74-L81](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L74-L81)）调用 `inflateEnd` 并释放内存；析构函数（[L55](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L55)）也走 `release()`，保证异常路径不泄漏。
- [L83-L121](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L83-L121)（`transform()`）：[L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L102) 以 `Z_NO_FLUSH` 增量调用 `inflate`；[L104-L106](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L104-L106) 见到 `Z_STREAM_END` 置 `finished_`（gzip 流自带结尾标记，不像 chunked 需要显式终块）；[L107-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L107-L109) 容忍 `Z_BUF_ERROR`（输入暂时不足的正常信号），其余错误码抛异常；[L113](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L113) 把本轮解压产物交给 delegate，[L114-L116](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L114-L116) 在输出缓冲未写满时退出循环（说明输入已耗尽）；[L118-L119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.cc#L118-L119) 用 `inlen - strm_->avail_in` 汇报消费量——libz 自己会留下没消费的输入在 `avail_in` 里。
- 输出缓冲大小 `OUTBUF_LENGTH = 16_k` 定义在 [src/GZipDecodingStreamFilter.h:L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GZipDecodingStreamFilter.h#L54)：16KiB 恰与 u3-l4 讲过的 block 大小一致，解压产出按这个粒度分批写盘。
- 整个文件被 `#ifdef HAVE_ZLIB` 包裹（见 [src/HttpResponse.cc:L214-L219](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponse.cc#L214-L219) 的使用点）——是否编译由构建系统探测 zlib 决定（u1-l2 的特性开关机制），没编进来时工厂恒返回 `nullptr`，走 4.3.3 的「原样保存」降级。
- 触发条件上，aria2 默认**不**主动请求压缩：`--http-accept-gzip` 选项默认 `false`（[src/OptionHandlerFactory.cc:L1097-L1105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L1097-L1105)），[src/HttpRequestCommand.cc:L104-L109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpRequestCommand.cc#L104-L109) 只有该选项为真才 `enableAcceptGZip()`，让请求带上 `Accept-Encoding: deflate, gzip`（选项文档见 [src/usage_text.h:L728-L729](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/usage_text.h#L728-L729)）。这是有意为之：解压会牺牲分段下载与预分配两个提速手段，只有用户明确要求才交换。

#### 4.4.4 代码实践

**实践目标**：对比开关 gzip 协商后，同一资源落到磁盘的字节是否不同。

**操作步骤**：

1. 用下面的示例代码起一个强制返回 gzip 内容的服务（预压缩一个文本文件 `hello.txt` 为 `hello.txt.gz`）：

   ```python
   # gzip_server.py（示例代码，非 aria2 项目自带）
   import http.server, gzip

   class H(http.server.BaseHTTPRequestHandler):
       def do_GET(self):
           body = b"hello stream filter\n" * 64
           gz = gzip.compress(body)
           self.send_response(200)
           self.send_header("Content-Encoding", "gzip")
           self.send_header("Content-Length", str(len(gz)))
           self.end_headers()
           self.wfile.write(gz)

   http.server.HTTPServer(("127.0.0.1", 8099), H).serve_forever()
   ```

2. 分别下载两次到不同目录：

   ```bash
   ./src/aria2c --http-accept-gzip=true -d out-a http://127.0.0.1:8099/hello.txt
   ./src/aria2c -d out-b http://127.0.0.1:8099/hello.txt
   ```

3. 用 `file out-a/hello.txt out-b/hello.txt` 与 `wc -c` 对比两份产物。

**需要观察的现象**：`out-a` 应得到解压后的明文（大小为 `body` 的原始长度）；`out-b` 应是 gzip 压缩字节（`file` 识别为 gzip 数据）。

**预期结果**：结合 4.3.3 的路径分析——`--http-accept-gzip=true` 时 `shouldInflateContentEncoding()` 为真，任务走 `handleOtherEncoding`，链上装入 `GZipDecodingStreamFilter`，落盘为明文；默认 false 时走 `handleDefaultEncoding`，只装 Sink，压缩字节原样保存。（本地运行结果待验证。）

#### 4.4.5 小练习与答案

**练习 1**：`inflateInit2` 的参数为什么是 47？改成 15 或 31 会怎样？

**答案**：47 = 15 + 32。15 是 zlib 窗口位数基准；+32 是 libz 的「自动探测头部格式」标志：输入带 gzip magic 就按 gzip 解，带 zlib 头就按 deflate(zlib) 解。只传 15 则只认 zlib 格式，遇到 gzip 流会报错；31 只认 gzip 格式，遇到 zlib 流会报错。

**练习 2**：为什么 `GZipDecodingStreamFilter::transform()` 里 `Z_STREAM_END` 之后不立即返回，还要继续把缓冲里剩余的解压产出转发完？

**答案**：`Z_STREAM_END` 只表示解压器见到了流的结尾标记，此时 16KiB 输出缓冲里可能还有最后一批未转发的解压数据。循环体会继续执行本轮的 `delegate->transform()`（L113 在判定之前），然后因 `avail_out > 0` 退出。若提前 return，文件尾部数据会丢失。

**练习 3**：chunked + gzip 同时存在时，`finished()` 在什么条件下才返回 true？

**答案**：链头 chunked 的 `finished()` 为真的条件是「自身到达 `CHUNKS_COMPLETE` **且** delegate（gzip）的 `finished()` 为真」，而 gzip 的 `finished()` 又要求「自身见过 `Z_STREAM_END` **且** 其 delegate（Sink）为真」（Sink 未覆写 `finished()` 语义上恒真）。两层「与」保证外壳与内核都完整解码后，整条链才算结束，`DownloadCommand` 才能宣布 segment 完成。

## 5. 综合实践

**任务**：亲手制造「边界错位」的 chunked 响应，验证状态机的半包免疫，再用日志看清状态迁移。

### 5.1 实践目标

把本讲三个模块串起来：写出会「故意拆坏边界」的 chunked 服务器（4.2），驱动 aria2 的完整下载链（4.3），并确认解码后文件逐字节无损。

### 5.2 操作步骤

1. **准备对照基准**：生成一个已知内容的文件并记录校验和。

   ```bash
   seq 1 20000 > payload.txt && sha256sum payload.txt
   ```

2. **编写「坏边界」服务器**（示例代码，非 aria2 项目自带）：每次 `write` 都刻意切在语法单元中间——大小数字 `"a"` 与 `"\r\n"` 分开发，块数据从中间劈开，终块的 `"0"`、`"\r\n"`、`"\r\n"` 也拆成三次发送。

   ```python
   # evil_chunked.py（示例代码）
   import http.server

   class H(http.server.BaseHTTPRequestHandler):
       protocol_version = "HTTP/1.1"

       def do_GET(self):
           data = open("payload.txt", "rb").read()
           self.send_response(200)
           self.send_header("Transfer-Encoding", "chunked")
           self.end_headers()
           # 把实体切成 10 字节的块；大小行、CRLF、数据、块尾全部错位发送
           for off in range(0, len(data), 10):
               chunk = data[off:off + 10]
               self.wfile.write(b"%x" % len(chunk))   # 只发大小数字
               self.wfile.flush()
               self.wfile.write(b"\r\n")              # 再发 CRLF
               self.wfile.flush()
               half = len(chunk) // 2
               self.wfile.write(chunk[:half])         # 数据前半
               self.wfile.flush()
               self.wfile.write(chunk[half:])         # 数据后半
               self.wfile.flush()
               self.wfile.write(b"\r")                # 块尾拆开发
               self.wfile.flush()
               self.wfile.write(b"\n")
               self.wfile.flush()
           for piece in (b"0", b"\r", b"\n", b"\r", b"\n"):   # 终块逐字节发
               self.wfile.write(piece)
               self.wfile.flush()

   http.server.HTTPServer(("127.0.0.1", 8100), H).serve_forever()
   ```

3. **下载并校验**：

   ```bash
   python3 evil_chunked.py &
   ./src/aria2c -d out -o payload.txt --log=chunk-log.txt --log-level=info \
       http://127.0.0.1:8100/payload.txt
   sha256sum out/payload.txt
   ```

4. **加状态日志**（实验后还原）：在 [src/ChunkedDecodingStreamFilter.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkedDecodingStreamFilter.cc) 的 `transform()` 里包一个状态迁移探针（示例代码）——在 for 循环末尾打印「旧状态 → 新状态」：

   ```cpp
   int before = state_;          // 放在 switch 之前记录
   // …… switch(state_) 原逻辑 ……
   // switch 结束后：
   if (state_ != before) {
     A2_LOG_DEBUG(fmt("chunked state %d -> %d (i=%zu)", before, state_, i));
   }
   ```

   重新编译后用 `--log-level=debug` 再下载一次，检索 `chunked state`。

### 5.3 需要观察的现象

- 第 3 步：`out/payload.txt` 与原始 `payload.txt` 的 sha256 完全一致，aria2 报告下载完成而非重试；
- 第 4 步：日志里能看到 `PREV_CHUNK_SIZE(0) → CHUNK_SIZE(1)` 这类迁移**有时发生在同一次 `transform()` 内**（一次 read 收到了 `"a\r\n"`），**有时跨越两次 `transform()`**（`"a"` 先到、`"\r\n"` 下一轮才到）——这正是边界错位被状态机消化的直接证据。

### 5.4 预期结果

文件逐字节完整说明：无论服务器如何拆分发送，状态机只关心字节序列的语法正确性，不关心到达边界；每个「半截」输入结束时，进度都安全地停在 `state_`/`chunkRemaining_` 里。若把校验和改为不一致或下载中途报 `Bad chunk encoding`，请对照 4.2.3 的异常抛出点排查是服务器语法写错（例如块尾少 CRLF）而非状态机问题。（全部运行结果待本地验证。）

## 6. 本讲小结

- `StreamFilter` 用「`init()` → 反复 `transform()` → `finished()` / `release()`」的流式契约加上 `delegate_` 委托链，把「解码」从下载循环中彻底解耦；`transform()` 返回值（写盘量）与 `getBytesProcessed()`（输入消费量）是两个必须分开的量。
- `ChunkedDecodingStreamFilter` 是 12 态逐字节状态机（枚举共 13 个值，`PREV_END_CR` 是无 case 的残留），解析进度全部外置在 `state_`/`chunkSize_`/`chunkRemaining_` 三个成员里，因此对任意粘包/半包免疫；`CHUNK` 状态批量转发块数据，终块后精确停在流末尾，多余字节留给连接上的下一个响应。
- 链的装配由响应头驱动：`HttpResponse` 两个工厂按 `Transfer-Encoding`/`Content-Encoding` 造过滤器，`HttpResponseCommand` 以「chunked 外层、gzip 内层」套好再交给 `DownloadCommand::installStreamFilter`，后者把构造时预装的 `SinkStreamFilter` 压到链尾；Transfer-Encoding 不支持即失败，Content-Encoding 不支持则降级为原样保存。
- `DownloadCommand::executeInternal` 按 `sinkFilterOnly_` 走两条路：纯搬运先算 `bufSize` 再喂链，有解码器则整段喂入、以 `getBytesProcessed()` 反推 drain 量；segment 的完成判定在非 Sink 链上额外要求 `finished()`（链式「与」），保证 chunked 尾巴与 gzip 结尾都被消费。
- gzip 解压由 `GZipDecodingStreamFilter` 封装 libz（`inflateInit2(47)` 自动识别格式），代价是禁用分段下载与文件预分配；aria2 默认不请求压缩，需 `--http-accept-gzip=true` 显式开启。

## 7. 下一步学习建议

本讲结束后，响应体已经过解码、走到了链尾 `SinkStreamFilter::transform()` 的 `out->writeData(...)`——`out` 是什么、数据如何真正落盘，正是下一单元的起点：

1. **u5-l1（DiskWriter 抽象）与 u5-l2（PieceStorage）**：`SinkStreamFilter` 里的 `segment->getPiece()`、`piece->appendWrCache`、`out->writeData` 分别通向 piece 缓存与 DiskAdaptor/DiskWriter 体系，建议按此顺序精读，把「解码后的字节」一路追到磁盘。
2. **u5-l6（写盘缓存）**：本讲多次出现的 `WrDiskCache` 参数（[src/DownloadCommand.cc:L101-L102](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L101-L102)）在那篇讲义展开。
3. **横向对照**：`HttpSkipResponseCommand`（[src/HttpResponseCommand.cc:L488-L514](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L488-L514)）在丢弃响应体时也复用同一条过滤链（只装 TE 层）——重定向跳 body 的正确性同样依赖 `bytesProcessed` 的精确记账，可作为一个小型的复习材料。
4. 若想检验掌握程度，回到综合实践，把服务器改成「块尾只发 `\n` 不发 `\r`」，先笔算状态机会在哪里抛异常，再对照日志验证。
