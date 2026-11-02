# u6-l1 torrent 元数据解析：bencode 与种子结构

## 1. 本讲目标

本讲是 BitTorrent 单元（单元 6）的第一讲，回答一个问题：**aria2 拿到一个 `.torrent` 文件后、还没连任何 peer 之前，都做了什么？**

学完本讲你应该能够：

1. 读懂 bencode 编码，并说出 aria2 用哪条调用链把 `.torrent` 字节流变成内存中的 `ValueBase` 树。
2. 跟踪 `bittorrent_helper.cc` 中 `processRootDictionary` 的完整流程：infohash 怎么算、piece 哈希怎么切、文件列表怎么变成 `FileEntry`、以及「piece 数量与总长必须吻合」的一致性校验。
3. 说出 `AnnounceList`/`AnnounceTier` 的两层结构，解释 aria2 如何用「顺序 + 移动规则」表达 tracker 的优先级（而不是数值权重）。
4. 列出 `TorrentAttribute` 承载的全部 BT 运行时信息，以及它如何挂载到 `DownloadContext` 的 `CTX_ATTR_BT` 属性槽上。

本讲只讲「静态元数据的解析」，不涉及任何网络交互——握手、peer 交换、announce 请求分别在后续讲义（u6-l3、u6-l4、u6-l7）中展开。

## 2. 前置知识

### 2.1 .torrent 文件是什么

`.torrent` 文件（俗称「种子」）是一个 bencode 编码的字典，描述「下载什么、从哪些 tracker 找人、每块数据校验和是什么」。一个最小种子的骨架是：

```text
d                          <- 字典开始
  8:announce               <- 键：字符串 "announce"，长度前缀 8
  30:http://tracker/announce
  4:info                   <- 键："info"，值是子字典（种子核心）
  d
    4:name5:hello          <- 文件名
    6:lengthi1024e         <- 文件长度：整数 1024
    12:piece lengthi262144e
    6:pieces<20×N 字节的二进制 SHA-1>
  e
e                          <- 字典结束
```

### 2.2 bencode 语法速览

bencode 只有四种类型，每种都有明确的定界符：

| 类型 | 编码 | 例子 | 解码结果 |
|---|---|---|---|
| 整数 | `i<十进制>e` | `i1024e` | 1024 |
| 字符串 | `<长度>:<字节>` | `5:hello` | `"hello"` |
| 列表 | `l<元素...>e` | `l5:helloi1ee` | `["hello", 1]` |
| 字典 | `d<键1><值1><键2><值2>...e` | `d1:ai1ee` | `{"a": 1}` |

两个重要约定：

- **字典的键必须是字符串，且按字节序排序**（规范要求）。这个约定后来成了 infohash 能「重新编码还原」的关键。
- 字符串是**原始字节串**，不是文本——`pieces` 字段里存的就是 20 字节一段的二进制 SHA-1，用文本编辑器打开种子会看到乱码，这是正常的。

### 2.3 infohash 与 SHA-1

infohash 是整个 `info` 子字典字节串的 SHA-1 摘要（20 字节）：

\[ \text{infoHash} = \mathrm{SHA1}(\mathrm{bencode}(\text{info 字典})) \]

它在 BT 网络里充当「内容身份证」：DHT 用它做键、magnet 链接里的 `xt=urn:btih:<hex>` 就是它的十六进制、tracker 请求里的 `info_hash` 参数也是它。**info 字典里任何一个字节变了，infohash 就变**，网络会把它当成另一个种子。

### 2.4 tracker 与 tier（层）

tracker 是一台 HTTP/UDP 服务器，客户端向它「报到」（announce）并换取其他下载者的地址。种子可以用两级结构描述多个 tracker：

- 外层：tier（层）列表。**层与层之间是主备关系**——第 1 层全挂了才轮到第 2 层。
- 内层：同一层内的 tracker 互为镜像，随便挑一个用。

这就是 `announce-list` 的结构：`[[t1, t2], [t3]]` 表示「先试 t1/t2 这层，不行再用 t3」。

### 2.5 与前面讲义的衔接

- u3-l2 讲过 `DownloadContext` 是任务元数据的聚合根，协议专属信息通过 `ContextAttribute`「空基类 + 枚举槽位」挂载，目前只有 `CTX_ATTR_BT` 一个槽——本讲就是这个槽的唯一使用者 `TorrentAttribute` 的诞生地。
- u3-l3 讲过 `Context` 构造函数按 torrent/metalink/input-file/URI 四级互斥分支调用 `download_helper` 的工厂函数；本讲下钻 `createRequestGroupForBitTorrent` 内部，看它如何调用 `bittorrent::load`。
- u9-l2 会系统讲 `ValueBase` 动态类型与解析器骨架，本讲只需知道 `ValueBase` 家族是 `String/Integer/List/Dict/Null/Bool`，`Dict` 内部是 `std::map<std::string, std::unique_ptr<ValueBase>>`（见 [src/ValueBase.h:L247-L249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L247-L249)，键天然有序）。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/bencode2.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L48-L126) / [src/bencode2.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.h#L44-L62) | bencode 的 decode/encode 门面：解码产出 `ValueBase` 树，编码用 visitor 遍历树输出字节串 |
| [src/GenericParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L45-L115) | 通用增量解析器骨架：`parseUpdate` 喂数据、`parseFinal` 收尾；`parseFile` 自由函数按 4KB 块读文件喂解析器 |
| [src/ValueBaseBencodeParser.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseBencodeParser.h#L46-L47) | `GenericParser` 的 bencode 特化别名，`bittorrent_helper` 与 `download_helper` 都用它 |
| [src/bittorrent_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L68-L1098) / [src/bittorrent_helper.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L60-L395) | 本讲主角：`load`/`loadFromMemory` 系列入口、`processRootDictionary` 总装流程、infohash/peer id 等工具、`--show-files` 用的 `print` 模板 |
| [src/AnnounceList.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L45-L278) / [src/AnnounceList.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.h#L50-L138) | 运行时 tracker 选择器：两层迭代器 + 成功/失败移动规则 |
| [src/AnnounceTier.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.cc#L39-L78) / [src/AnnounceTier.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.h#L45-L71) | 单个 tier：URL 队列 + announce 事件状态机 |
| [src/TorrentAttribute.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.cc#L39-L47) / [src/TorrentAttribute.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h#L48-L69) | BT 专属信息的纯数据结构（struct） |
| [src/Context.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L89-L95) | `--show-files` 处理入口 `showTorrentFile`：构造空 Option + DownloadContext 后调 `bittorrent::load` 再 `print` |
| [src/download_helper.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L186-L347) | 任务创建侧：`createBtRequestGroup` 调 `loadFromMemory`，`createRequestGroupForBitTorrent` 先 bencode 解码再组装 |
| [test/BittorrentHelperTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L140-L148) / [test/AnnounceListTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AnnounceListTest.cc#L111-L170) / [test/test.torrent](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/test.torrent) | 实践素材：内置测试种子与断言，可直接用来验证你的理解 |

`test/test.torrent` 是仓库自带的迷你种子（约 300 字节），本讲大量实践都基于它。它的原始内容是：

```text
d8:announce36:http://aria.rednoah.com/announce.php13:announce-listll16:http://tracker1 
el15:http://tracker2el15:http://tracker3ee7:privatei1e7:comment17:REDNOAH.COM RULES
13:creation datei1123456789e10:created by5:aria24:infod5:filesld6:lengthi284e4:pathl5:aria2
3:src6:aria2ceed6:lengthi100e4:pathl19:aria2-0.2.2.tar.bz2eee4:name10:aria2-test
12:piece lengthi128e6:pieces60:<AAA...BBB...CCC...>ee
```

注意两个细节：第一个 tracker 字符串长度前缀是 16（`http://tracker1` 只有 15 字符，**末尾多一个空格**，这是测试故意埋的，用来验证解析时的 `strip`）；`announce-list` 是三个单元素 tier，而不是一个三元素 tier。

## 4. 核心概念与源码讲解

### 4.1 bencode2：种子的编解码器

#### 4.1.1 概念说明

`bencode2` 命名空间只有两个动词：`decode`（字节串 → `ValueBase` 树）和 `encode`（`ValueBase` 树 → 字节串）。它是纯函数式的工具层，不知道「torrent」为何物——DHT 报文（u7-l2）、BT 扩展消息（u6-l8）也都用它。

为什么需要「编码」方向？两个用途都和本讲强相关：

1. **计算 infohash**：拿到解析后的 `info` 子字典后要重新编码成字节串再做 SHA-1。
2. **把 magnet 下载到的 metadata 还原成种子**：`metadata2Torrent`（u6-l9 详讲）。

#### 4.1.2 核心流程

解码不是 `bencode2` 自己做的，而是委托给一套「解析器骨架 + 状态机」组合（骨架细节在 u9-l2 展开）：

```text
.torrent 文件
   │  GenericParser.h::parseFile()          按 4KB 块读文件
   ▼
ValueBaseBencodeParser (= GenericParser<BencodeParser, ValueBaseStructParserStateMachine>)
   │  parseUpdate() × N  →  增量喂入
   │  parseFinal()       →  收尾并取结果
   ▼
ValueBase 树 (Dict/List/Integer/String)
```

编码方向则是 visitor 模式：对树的每个节点调用对应 `visit` 重载，按 bencode 文法逐段写入 `ostringstream`。

#### 4.1.3 源码精读

先看解码入口。`decode` 的核心重载在 [src/bencode2.cc:L63-L76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L63-L76)：构造一个 `ValueBaseBencodeParser`，把整段数据一次性喂给 `parseFinal`；返回值 `error` 为负数说明文法错误，抛出错误码 `BENCODE_PARSE_ERROR` 的异常，否则它记录「实际消费的字节数」（bencode 允许尾部有多余字节，这个数在 magnet 元数据处理里很有用）：

```cpp
std::unique_ptr<ValueBase> decode(const unsigned char* data, size_t len,
                                  size_t& end)
{
  ssize_t error;
  bittorrent::ValueBaseBencodeParser parser;
  auto res = parser.parseFinal(reinterpret_cast<const char*>(data), len, error);
  if (error < 0) {
    throw DL_ABORT_EX2(
        fmt("Bencode decoding failed: error=%d", static_cast<int>(error)),
        error_code::BENCODE_PARSE_ERROR);
  }
  end = error;
  return res;
}
```

`ValueBaseBencodeParser` 本身只是个别名，见 [src/ValueBaseBencodeParser.h:L46-L47](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBaseBencodeParser.h#L46-L47)：

```cpp
typedef GenericParser<BencodeParser, ValueBaseStructParserStateMachine, true>
    ValueBaseBencodeParser;
```

第三个模板参数 `allowEmptyName = true` 允许字典出现空字符串键（对非规范种子更宽容）。

从文件读种子的路径在 [src/GenericParser.h:L91-L115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L91-L115) 的 `parseFile`：打开文件（容忍 `EINTR` 重试）、按 `4_k`（4KB）块循环 `read` → `parser.parseUpdate`，最后 `parseFinal(0, 0, nproc)` 收尾。**增量式**设计的意义是：同一套骨架既能在启动时一次喂完整个文件，也能在 DHT/网络流场景边收边解析。

编码方向 [src/bencode2.cc:L78-L126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L78-L126) 是一个 visitor：`String` 输出 `<len>:` 加原始字节；`Integer` 输出 `i<value>e`；`List`/`Dict` 分别用 `l...e`/`d...e` 包裹并递归。关键一行是 `Dict` 分支直接遍历内部 map：

```cpp
virtual void visit(const Dict& dict) CXX11_OVERRIDE
{
  out_ << "d";
  for (const auto& e : dict) {
    auto& key = e.first;
    out_ << key.size() << ":";
    out_.write(key.data(), key.size());
    e.second->accept(*this);
  }
  out_ << "e";
}
```

由于 `Dict` 的底层容器是 `std::map`（[src/ValueBase.h:L247-L249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L247-L249)），遍历顺序天然是键的字典序——**重新编码的输出与「键已排序的规范种子」逐字节一致**。这正是下一节 infohash 能用「重编码」而不是「切原文」来实现的前提。

#### 4.1.4 代码实践：手工解码 test.torrent

1. **实践目标**：不借助 aria2，亲手把 `test/test.torrent` 的 announce-list 部分解出来，确认它是「三个单元素 tier，且第一个 URL 带尾部空格」。
2. **操作步骤**：
   - 用编辑器打开 `test/test.torrent`（它全是 ASCII，可直接读）。
   - 定位 `13:announce-list` 之后的内容，逐字符手工切分：`l`（外层列表）→ `l`（tier1）→ `16:`（读 16 字节）`http://tracker1 ` → `e`（tier1 结束）→ `l` `15:http://tracker2` `e` → `l` `15:http://tracker3` `e` → `e`（外层结束）。
   - 想偷懒的话，可以用下面这段「示例代码」（独立小脚本，与 aria2 无关）自动解码：

     ```python
     # 示例代码：最小 bencode 解码器
     def decode(data, pos):
         c = data[pos:pos+1]
         if c == b'i':                              # 整数 i123e
             end = data.index(b'e', pos)
             return int(data[pos+1:end]), end + 1
         if c == b'l':                              # 列表 l...e
             pos, out = pos + 1, []
             while data[pos:pos+1] != b'e':
                 v, pos = decode(data, pos)
                 out.append(v)
             return out, pos + 1
         if c == b'd':                              # 字典 d...e
             pos, out = pos + 1, {}
             while data[pos:pos+1] != b'e':
                 k, pos = decode(data, pos)
                 v, pos = decode(data, pos)
                 out[k] = v
             return out, pos + 1
         end = data.index(b':', pos)                # 字符串 <len>:...
         n = int(data[pos:end])
         return data[end+1:end+1+n], end + 1 + n

     raw = open('test/test.torrent', 'rb').read()
     tree, _ = decode(raw, 0)
     print(tree[b'announce-list'])
     ```

3. **需要观察的现象**：第一个 tier 的元素是 `b'http://tracker1 '`（16 字节，带尾部空格），后两个分别是 `b'http://tracker2'`、`b'http://tracker3'`。
4. **预期结果**：输出形如 `[[b'http://tracker1 '], [b'http://tracker2'], [b'http://tracker3']]`。这个尾部空格会在 4.2 节的 `extractAnnounce` 里被 `util::strip` 去掉——测试 [test/BittorrentHelperTest.cc:L293-L295](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L293-L295) 断言解析后是干净的 `http://tracker1`。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`i-1e`、`0:`、`le`、`de` 分别解码成什么？

答案：整数 `-1`；空字符串 `""`；空列表 `[]`；空字典 `{}`。bencode 允许所有「空」容器，aria2 的解析器也接受（`allowEmptyName = true` 甚至允许空键）。

**练习 2**：为什么 `bencode2::encode` 不需要「排序」这个步骤？

答案：因为 `ValueBase::Dict` 底层就是 `std::map<std::string, ...>`，遍历即有序；而 bencode 规范本身要求字典键排序，两者恰好对齐，重编码输出与规范种子原文一致。

**练习 3**：`decode(data, len, end)` 里第三个参数 `end` 有什么用？

答案：返回**实际消费的字节数**。bencode 流里顶层元素后面可以跟别的数据（例如 ut_metadata 分片传输），调用方靠 `end` 知道从哪里继续切。

### 4.2 bittorrent_helper：从根字典到 DownloadContext

#### 4.2.1 概念说明

`bittorrent` 命名空间下的 `load`/`loadFromMemory` 家族是种子解析的总入口。无论种子来自磁盘文件、RPC 传入的字节串、magnet 拼出的 ValueBase，最终都汇入同一个私有函数 `processRootDictionary`：它做完整性校验、算 infohash、把公共信息（文件列表、piece 长度、piece 哈希）写进 `DownloadContext`，把 BT 专属信息（announce、nodes、私有标志等）装进 `TorrentAttribute` 再挂到属性槽。

一句话概括分工：**`DownloadContext` 存「所有协议都关心的元数据」，`TorrentAttribute` 存「只有 BT 关心的元数据」**。

#### 4.2.2 核心流程

`processRootDictionary` 的执行顺序（也是下文源码精读的顺序）：

```text
1. 校验根节点是 Dict、含 "info" 子 Dict          否则 → BITTORRENT_PARSE_ERROR
2. 对 info 子字典重新 bencode 编码 → SHA-1 → infoHash
   同时把编码字节串存进 metadata/metadataSize（给 magnet 场景用）
3. 读 "pieces"        → 每 20 字节切一个 piece 哈希，写入 ctx
4. 读 "piece length"  → 校验非负，写入 ctx
5. 读 "private"       → privateTorrent 标志（禁 DHT/PEX 的私有种子）
6. 读 "url-list"      → HTTP Seeding 的 web-seed URI，并入 FileEntry 的 URI 列表
7. extractFileEntries → 生成 FileEntry 列表（单文件/多文件两条路径）
8. 一致性校验：ceil(总长 / piece length) == piece 哈希个数
9. extractAnnounce    → announce-list（或退化的 announce）→ announceList
10. extractNodes      → DHT 引导节点列表
11. 读 creation date / comment / created by
12. ctx->setAttribute(CTX_ATTR_BT, torrent)  ← 挂载属性槽
```

其中第 8 步的公式（整数除法即向上取整）：

\[ \text{期望 piece 数} = \left\lceil \frac{\text{totalLength}}{\text{pieceLength}} \right\rceil = \frac{\text{totalLength} + \text{pieceLength} - 1}{\text{pieceLength}} \]

#### 4.2.3 源码精读

**入口家族**。`load` 有两个重载（[src/bittorrent_helper.cc:L531-L549](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L531-L549)），差别只在于是否携带一组辅助 URI（`--torrent-file` 命令行上额外给的 web-seed）：

```cpp
void load(const std::string& torrentFile,
          const std::shared_ptr<DownloadContext>& ctx,
          const std::shared_ptr<Option>& option,
          const std::string& overrideName)
{
  ValueBaseBencodeParser parser;
  processRootDictionary(ctx, parseFile(parser, torrentFile).get(), option,
                        torrentFile, overrideName, std::vector<std::string>());
}
```

注意 `parseFile` 来自 [src/GenericParser.h:L92-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L92-L94)（`aria2::parseFile`，ADL 找到），`.get()` 把 `unique_ptr<ValueBase>` 降级为裸指针——**`ValueBase` 树在这里之后由谁拥有？** 答案：解析结果被逐字段「搬空」后丢弃，存活下来的是 `DownloadContext` 里的字段与 `TorrentAttribute`。

`loadFromMemory` 家族（[src/bittorrent_helper.cc:L551-L601](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L551-L601)）覆盖四种来源：内存字节、`std::string`、已经是 `ValueBase*` 的树（RPC `aria2.addTorrent` 走这条），全部汇入 `processRootDictionary`。任务创建侧 `createBtRequestGroup` 用的就是 ValueBase 版本，见 [src/download_helper.cc:L194-L197](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L194-L197)：

```cpp
auto dctx = std::make_shared<DownloadContext>();
// may throw exception
bittorrent::loadFromMemory(torrent, dctx, option, auxUris,
                           metaInfoUri.empty() ? "default" : metaInfoUri);
```

**总装车间 `processRootDictionary`** 定义在 [src/bittorrent_helper.cc:L413-L528](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L413-L528)。开头两个防御检查（L420-L429）：根不是字典、没有 `info` 键，都抛错误码 `BITTORRENT_PARSE_ERROR` 的异常（对应退出码层面的解析失败）。

**infohash 计算**（L432-L440）是全文件最关键的 9 行：

```cpp
// retrieve infoHash
std::string encodedInfoDict = bencode2::encode(infoDict);
unsigned char infoHash[INFO_HASH_LENGTH];
message_digest::digest(infoHash, INFO_HASH_LENGTH,
                       MessageDigest::sha1().get(), encodedInfoDict.data(),
                       encodedInfoDict.size());
torrent->infoHash.assign(&infoHash[0], &infoHash[INFO_HASH_LENGTH]);
torrent->metadata = encodedInfoDict;
torrent->metadataSize = encodedInfoDict.size();
```

三件事一次完成：重编码 info 字典 → SHA-1 得 20 字节原始摘要（`INFO_HASH_LENGTH = 20`，见 [src/BtConstants.h:L43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L43)）→ 原始字节存进 `infoHash`（注意**不是**十六进制文本；要文本用 `getInfoHashString`，见 [src/bittorrent_helper.cc:L629-L632](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L629-L632) 的 `util::toHex`）。`metadata`/`metadataSize` 把 info 字典原文留在手边——magnet 下载（u6-l9）拿到元数据后要靠它校验、落盘成 `.torrent`。

**piece 哈希与 piece 长度**（L443-L474）：`pieces` 是一根长字节串，每 `PIECE_HASH_LENGTH = 20` 字节（[src/BtConstants.h:L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L45)）是一个 piece 的 SHA-1。个数就是长度整除：`numPieces = piecesData->s().size() / PIECE_HASH_LENGTH`。源码里两段被注释掉的「空 pieces / 零 piece 报错」逻辑是刻意保留的——为了支持 0 字节的怪种子（L448-L456 的注释写明 "Commented out To download 0 length torrent"）。`piece length` 读出后校验非负（L465-L469），然后交给 `extractPieceHash`（[src/bittorrent_helper.cc:L99-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L99-L110)）按 20 字节切片、以 `"sha-1"` 为算法名写入 `ctx->setPieceHashes`——这就是 u5-l5 分块校验数据的来源。

**私有种子与 url-list**（L476-L493）：`private` 为整数 1 时置 `privateTorrent = true`（这种种子禁 DHT 与 PEX，u6-l2、u6-l8 会消费这个标志）。`url-list` 遵循 HTTP Seeding 规范（源码注释给出 http://www.getright.com/seedtorrent.html），由 `extractUrlList`（[src/bittorrent_helper.cc:L114-L157](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L114-L157)）解析——它用 visitor 同时兼容「单个字符串」和「字符串列表」两种写法，随后与命令行额外 URI 合并、排序去重。

**文件条目**由 `extractFileEntries` 生成（[src/bittorrent_helper.cc:L178-L347](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L178-L347)），骨架是：

1. 定名字（L185-L211）：`name.utf-8` 优先于 `name`；两者皆无则退化为种子文件名 + `.file`。名字或路径要过 `util::detectDirTraversal` 检查——**恶意种子可以用 `../../` 逃出下载目录**，这里抛 `MSG_DIR_TRAVERSAL_DETECTED`。非 UTF-8 字节经 `util::encodeNonUtf8` 百分号转义。
2. 多文件分支（L214-L300）：有 `files` 列表即 `BT_FILE_MODE_MULTI`。逐文件读 `length` 与 `path`（`path.utf-8` 优先），把根目录名插到 `pathelem[0]`，拼成 `根/子/文件` 路径；**`offset` 逐文件累加**，把多文件首尾相接成一条逻辑偏移空间——这正是 u3-l2 讲过的「多文件任务的总长 = 尾元素末偏移」的诞生地。每个 `FileEntry` 还会带上 url-list 拼出的 web-seed URI。
3. 单文件分支（L301-L341）：无 `files` 即 `BT_FILE_MODE_SINGLE`，读顶层 `length` 造一个偏移 0 的 `FileEntry`。
4. 收尾（L342-L346）：`ctx->setFileEntries(...)`；多文件模式再 `setBasePath`（根目录名），它是 u5-l3 `MultiDiskAdaptor` 摊平目录树的依据。

**一致性校验**（L498-L501）：

```cpp
if ((ctx->getTotalLength() + pieceLength - 1) / pieceLength != numPieces) {
  throw DL_ABORT_EX2("Too few/many piece hash.",
                     error_code::BITTORRENT_PARSE_ERROR);
}
```

piece 哈希个数必须恰好等于总长向上取整的 piece 数。以 `test.torrent` 为例：总长 284 + 100 = 384，piece length 128，\((384 + 128 - 1) \div 128 = 3\)，而 `pieces` 长 60 字节 = 3 个哈希，恰好通过。

**announce 与 nodes**（L503-L505）分别委托给下面两个小函数，详见 4.3 与 4.4 节。**杂项**（L507-L525）：`creation date`（整数时间戳）、`comment`/`comment.utf-8`、`created by`。最后一步挂载属性槽（L527）：`ctx->setAttribute(CTX_ATTR_BT, std::move(torrent))`——`CTX_ATTR_BT` 是 [src/ContextAttribute.h:L46-L52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ContextAttribute.h#L46-L52) 里枚举的第一个也是唯一一个槽位。

#### 4.2.4 代码实践：加日志观察解析结果

1. **实践目标**：在解析函数里加一行日志，打印 infohash 与 announce 层数，验证与 `--show-files` 的控制台输出一致。
2. **操作步骤**：
   - 先跑基线：`./src/aria2c -S test/test.torrent`（`-S` 是 `--show-files` 的短选项），记下输出中的 `Info Hash:` 行。
   - 在 [src/bittorrent_helper.cc:L497](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L497)（`extractFileEntries` 调用之后）临时插入一行日志（示例代码，验证后请还原）：

     ```cpp
     A2_LOG_INFO(fmt("parsed torrent: infohash=%s, tiers=%zu, pieces=%zu",
                     util::toHex(torrent->infoHash).c_str(),
                     torrent->announceList.size(), numPieces));
     ```

     注意此位置 `extractAnnounce` 尚未执行、`announceList` 还是空的——这本身就是个发现（解析顺序：文件条目先于 announce）。要看 tier 数应把这行日志加在 [L505](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L505)（`extractNodes` 调用）之后。
   - 重新 `make -j$(nproc)`（只改了 `bittorrent_helper.cc`，增量编译很快），带 `--log` 再跑一次：

     ```bash
     ./src/aria2c -S test/test.torrent --log=bt.log --log-level=info >/dev/null
     grep "parsed torrent" bt.log
     ```
3. **需要观察的现象**：日志里的 infohash 十六进制与 `-S` 输出的 `Info Hash:` 行完全一致；tiers 数为 3。
4. **预期结果**：infohash 为 `248d0a1cd08284299de78d5c1ed359bb46717d8c`——这不是猜的，单元测试 [test/BittorrentHelperTest.cc:L140-L148](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L140-L148) 断言了同样的值。piece 数为 3、tier 数为 3。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：aria2 算 infohash 时为什么「重新编码」而不是「从原文件里把 info 字典的字节区间切出来」？

答案：增量解析器得到的是树，不保留字节偏移。重新编码可行依赖两个事实：bencode 规范要求字典键排序，而 `ValueBase::Dict` 用 `std::map` 存储、遍历即有序，所以对规范种子而言重编码与原文字节一致。（推论：若某种子键未排序，`SHA1(原文)` 与 `SHA1(重编码)` 会不同——但那种种子本身就不规范，BT 网络以规范编码为准。）

**练习 2**：一个总长 1000、piece length 512 的种子需要几个 piece 哈希？`pieces` 字段多少字节？

答案：\(\lceil 1000/512 \rceil = 2\) 个哈希；`pieces` 长 \(2 \times 20 = 40\) 字节。若只有 39 字节，`processRootDictionary` 的一致性校验会抛 "Too few/many piece hash."。

**练习 3**：`extractFileEntries` 里对 `name` 和 `path` 调 `detectDirTraversal` 防的是什么攻击？

答案：目录穿越。恶意种子的文件路径可以写成 `../../etc/cron.d/evil`，若不拦截就会把文件写到下载目录之外。aria2 在名字与每个文件路径两处都检查，命中即拒绝解析（错误码 `BITTORRENT_PARSE_ERROR`）。

### 4.3 AnnounceList 与 AnnounceTier：多 tracker 层级模型

#### 4.3.1 概念说明

`TorrentAttribute::announceList` 只是 `vector<vector<string>>` 的静态快照；真正**在运行中决定「下一次 announce 用哪个 tracker」**的是 `AnnounceList`。它把两级列表包装成「双层迭代器 + 移动规则」：

- 外层 `tiers_`：`deque<shared_ptr<AnnounceTier>>`，每个 `AnnounceTier` 持有一个 `urls` 队列和一个 `event` 状态（[src/AnnounceTier.h:L45-L58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.h#L45-L58)）。
- 内层 `currentTracker_`：指向当前 tier 里下一个要用的 URL。

关于学习目标里的「tracker 权重」要说清楚：**aria2 没有数值权重字段**。优先级完全由三件事表达——① tier 的排列顺序（前面的层先用）；② 层内 URL 的顺序（会被 `shuffle` 随机打乱，避免所有客户端挤同一个 tracker）；③ 成功/失败后的移动规则（成功者置顶、失败者降级）。这是「用数据结构位置表达优先级」的典型设计。

#### 4.3.2 核心流程

```text
构造：torrentAttrs->announceList (vector<vector<string>>)
   │  AnnounceList::reconfigure()  把每层变成 AnnounceTier，丢弃空层
   ▼
初始指针：currentTier_ = 第一层, currentTracker_ = 层内第一个 URL
   │
   ├─ getAnnounce()       → 取当前 URL（未初始化或全失败时返回空串）
   │
   ├─ announceSuccess()   → 当前 URL 挪到本层队首；指针回到第 1 层第 1 个
   │
   ├─ announceFailure()   → 层内右移；层尽头 → 层右移；
   │                        所有层用尽 → currentTrackerInitialized_ = false
   │                        （allTiersFailed() 为真，需 resetTier() 重来）
   │
   └─ resetTier()         → 回到第 1 层第 1 个，重新开始一轮
```

tier 自带一个 announce 事件状态机（为什么要记录事件：tracker 协议要求客户端报到时声明 `started/stopped/completed` 等事件，u6-l7 详讲）：

```text
STARTED ──────────► DOWNLOADING      （首次报到成功，进入常规轮询）
STARTED_AFTER_COMPLETION ─► SEEDING  （做种恢复）
COMPLETED ────────► SEEDING          （刚下完，转做种）
STOPPED ──────────► HALTED           （客户端退出，报到结束）
```

#### 4.3.3 源码精读

**从静态到运行时**。构造与填充在 [src/AnnounceList.cc:L45-L83](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L45-L83)：`reconfigure(vector<vector<string>>)` 逐层造 `AnnounceTier`，**空层直接跳过**——这是 4.2 节 `extractAnnounce` 丢弃空 tier 之后的第二道保险。真正的消费者是 `DefaultBtAnnounce`，它在构造函数初始化列表里一行完成转换（[src/DefaultBtAnnounce.cc:L70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L70)）：

```cpp
announceList_(bittorrent::getTorrentAttrs(downloadContext)->announceList),
```

**指针初始化**（[src/AnnounceList.cc:L85-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L85-L95)）：`resetIterator` 把两层指针摆到开头；若第一层就没有 URL，则 `currentTrackerInitialized_ = false`，此后 `getAnnounce()` 一律返回空串（[L97-L105](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L97-L105)）——「无 tracker 种子」因此天然合法，只能靠 DHT/PEX 找 peer。

**成功路径**（[src/AnnounceList.cc:L107-L117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L107-L117)）：三步——`nextEvent()` 推进事件机；把当前 URL `erase` 后 `push_front` 回本层队首（**成功的 tracker 升为层内第一**）；指针整体回到第 1 层第 1 个（**成功即「归位」**，下次还是从全局最优开始）：

```cpp
void AnnounceList::announceSuccess()
{
  if (currentTrackerInitialized_) {
    (*currentTier_)->nextEvent();
    auto url = *currentTracker_;
    (*currentTier_)->urls.erase(currentTracker_);
    (*currentTier_)->urls.push_front(std::move(url));
    currentTier_ = std::begin(tiers_);
    currentTracker_ = std::begin((*currentTier_)->urls);
  }
}
```

**失败路径**（[src/AnnounceList.cc:L119-L135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L119-L135)）：层内 `++currentTracker_`；走到层尾则 `nextEventIfAfterStarted()` 推进事件并 `++currentTier_` 换层；所有层用尽就把 `currentTrackerInitialized_` 置 false，同时 `currentTier_` 停在 `end(tiers_)`——这正是 `allTiersFailed()` 的判定（[L252-L255](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L252-L255)）。之后可 `resetTier()` 重来一轮（等价于重新 `resetIterator`）。

**层内洗牌**（[src/AnnounceList.cc:L243-L250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L243-L250)）：`shuffle` 对每层 URL 用全局 `SimpleRandomizer` 洗牌。注意它**不打乱层间顺序**——主备关系保持，只随机层内镜像的选择。

**事件的层级过滤**。`stopped`/`completed` 事件不是每层都能发：两个谓词 functor（[src/AnnounceList.cc:L174-L206](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L174-L206)）按 `event` 状态筛层，`countStoppedAllowedTier`/`countCompletedAllowedTier`（[L208-L218](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L208-L218)）计数，`moveToStoppedAllowedTier`（[L229-L241](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L229-L241)）用 `find_wrap_if` 从当前位置**环形**找一个可发该事件的层。直觉：已经 `HALTED` 的层不该再收 stopped，刚 STARTED 的层不该收 completed。

**tier 的事件状态机**在 [src/AnnounceTier.cc:L46-L78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.cc#L46-L78)：`nextEvent` 按上文迁移表推进；`nextEventIfAfterStarted` 只处理 `STOPPED→HALTED`、`COMPLETED→SEEDING` 两条——它是「整层 tracker 都失败、被迫换层」时调用的，语义上只允许已经工作过的层发生状态跃迁。

**announce 数据的提取**回到 `extractAnnounce`（[src/bittorrent_helper.cc:L351-L380](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L351-L380)）：**`announce-list` 存在则完全忽略顶层 `announce`**；每层收集字符串时做 `strip`（去首尾空白——test.torrent 那个尾部空格就在这里被吃掉）加 `encodeNonUtf8`，空层丢弃。没有 `announce-list` 时退化为单 URL 组成一个单元素层。另外解析完成后还有一次**选项驱动的增删**：`adjustAnnounceUri`（[src/bittorrent_helper.cc:L1069-L1082](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1069-L1082)）按 `--bt-exclude-tracker`（`removeAnnounceUri`，[L1029-L1057](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1029-L1057)，支持 `*` 通配全删）和 `--bt-tracker`（`addAnnounceUri`，[L1059-L1067](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1059-L1067)，**每个追加 URI 独立成层**）修正 announceList。

#### 4.3.4 代码实践：推演 AnnounceListTest

1. **实践目标**：不看断言，先手工推演两个测试用例中 `getAnnounce()` 的返回序列，理解移动规则。
2. **操作步骤**：
   - 打开 [test/AnnounceListTest.cc:L145-L170](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AnnounceListTest.cc#L145-L170)（`testSingleAndMulti`），输入是 `ll8:tracker18:tracker2el8:tracker3ee`，即层结构 `[[tracker1, tracker2], [tracker3]]`。
   - 只看调用序列 `getAnnounce / announceSuccess / announceFailure ...`，在纸上写下你预期的每次返回值，再对照断言。
   - 也可以直接跑测试验证（在构建目录）：`make check TESTS=aria2-test` 后关注 `AnnounceListTest`，或用 `test/aria2-test` 配合 CppUnit 的测试名过滤。
3. **需要观察的现象**：`announceSuccess()` 之后 `getAnnounce()` 返回的是 **tracker1**（成功者置顶+归位），而失败推进是「层内优先、层尽换层」。
4. **预期结果**：与源码断言一致——依次 `tracker1` → (success) `tracker1` → (failure) `tracker2` → (failure) `tracker3` → (success) `tracker1`。最后一步正是注释 "tracker1 returns because after the announce to tracker3 succeeds, list pointer points to the first list" 所说的归位行为。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：种子只有顶层 `announce` 没有 `announce-list`，`AnnounceList` 里会有几层？

答案：一层，且层内一个 URL。`extractAnnounce` 的退化分支（[src/bittorrent_helper.cc:L372-L379](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L372-L379)）把单 URI 包成一个单元素层，后续逻辑无需特判。仓库里的 `test/single.torrent` 就是这种种子（测试 [test/BittorrentHelperTest.cc:L272-L283](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/BittorrentHelperTest.cc#L272-L283) 断言 1 层 1 URL）。

**练习 2**：为什么 `announceFailure` 在换层前要调 `nextEventIfAfterStarted()` 而不是 `nextEvent()`？

答案：换层发生在「本层 URL 全部失败」时。`nextEventIfAfterStarted` 只允许 `STOPPED→HALTED`、`COMPLETED→SEEDING` 两条跃迁，不会把还没成功报到过的层（STARTED）错误推进到 DOWNLOADING；而 `nextEvent` 无条件按表推进，会把「从未成功」的层标成已进入常规轮询，语义错误。

**练习 3**：`--bt-tracker=a,b` 追加的两个 tracker 在 `AnnounceList` 里如何排布？

答案：各自独立成层（`addAnnounceUri` 对每个 URI 单独 `push_back` 一层），排在种子原有层之后。它们不会并入已有层，因为它们与原层 tracker 没有镜像关系，失败退避也应独立进行。

### 4.4 TorrentAttribute：BT 运行时信息的载体

#### 4.4.1 概念说明

`TorrentAttribute` 是个纯数据 struct（没有行为），继承自空基类 `ContextAttribute`。它是「BT 协议在一颗种子里的一切私有信息」的集合体，也是 `DownloadContext` 属性槽 `CTX_ATTR_BT` 的唯一住户。把它与 u3-l2 的 `DownloadContext` 职责对照记忆：**文件列表、piece 长度、piece 哈希等「通用下载语义」放 ctx；infohash、announce、nodes 等「BT 语义」放属性**。

#### 4.4.2 核心流程

`TorrentAttribute` 的两条生命路径：

```text
路径 A（本讲）：.torrent / addTorrent / magnet 元数据
   processRootDictionary 填充全部字段
   → ctx->setAttribute(CTX_ATTR_BT, torrent)

路径 B（u6-l9 预告）：magnet:?xt=urn:btih:...
   parseMagnet 只填 3 个字段（infoHash / name / announceList）
   → loadMagnet 挂到 ctx
   → 元数据从 peer 处收齐后，再走路径 A 补全其余字段
```

#### 4.4.3 源码精读

**字段清单**（[src/TorrentAttribute.h:L48-L69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h#L48-L69)）逐个对应种子的来源：

| 字段 | 类型 | 来源/含义 |
|---|---|---|
| `name` | `string` | info 的 `name`/`name.utf-8`，多文件模式即根目录名 |
| `mode` | `BtFileMode` | `SINGLE`/`MULTI`/`NONE`，由有无 `files` 决定；`getModeString`（[src/bittorrent_helper.cc:L1084-L1094](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1084-L1094)）转文本 |
| `announceList` | `vector<vector<string>>` | 4.3 节的静态层级快照 |
| `nodes` | `vector<pair<string, uint16_t>>` | `nodes` 字段的 DHT 引导节点，端口合法范围 (0, 65536)（[src/bittorrent_helper.cc:L384-L409](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L384-L409)） |
| `infoHash` | `string` | 20 字节**原始** SHA-1（注释明确 "raw hash value 20 bytes"） |
| `metadata` / `metadataSize` | `string` / `size_t` | info 字典的 bencode 原文及其长度，magnet 场景的核心（u6-l9） |
| `privateTorrent` | `bool` | info 的 `private == 1` |
| `creationDate` / `comment` / `createdBy` | — | 根字典的三个杂项 |
| `urlList` | `vector<string>` | HTTP Seeding 的 web-seed URI |

构造函数只做零值初始化（[src/TorrentAttribute.cc:L39-L45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.cc#L39-L45)）。整个类型禁止拷贝（` TorrentAttribute(const TorrentAttribute&) = delete`），一律经 `shared_ptr` 共享。

**读取便捷函数**。全代码库取 BT 属性都走这两个窄门（[src/bittorrent_helper.cc:L603-L611](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L603-L611) 与 [L624-L632](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L624-L632)）：`getTorrentAttrs` 做属性槽的 `static_cast`（头文件 L157-L159 特意注释 "Don't allow copying"，提醒不要把返回值装进 `shared_ptr`——那会导致二次释放），`getInfoHashString` 把原始 20 字节转十六进制文本：

```cpp
std::string getInfoHashString(DownloadContext* dctx)
{
  return util::toHex(getTorrentAttrs(dctx)->infoHash);
}
```

**magnet 路径**。`parseMagnet`（[src/bittorrent_helper.cc:L901-L960](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L901-L960)）解析 `magnet:?xt=urn:btih:...&dn=...&tr=...`：`xt` 支持 40 位十六进制与 32 位 Base32 两种 infohash 写法；每个 `tr` 参数独立成层塞进 `announceList`；`name` 缺省拼成 `[METADATA]<infohash>`。`loadMagnet`（[L962-L966](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L962-L966)）把它挂上属性槽——此时除 infoHash/name/announceList 外全是零值，「文件多长、几个 piece、叫什么」统统未知，这正是 u6-l9「先下元数据再下数据」两阶段任务的起点。

**反向转换**。`torrent2Magnet`（[src/bittorrent_helper.cc:L993-L1014](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L993-L1014)）把属性拼回 magnet 链接（infohash 转大写 hex、`dn`/`tr` 百分号编码）；`metadata2Torrent`（[L968-L991](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L968-L991)）用 `metadata` 原文拼出 `d13:announce-list...4:info<metadata>e` 的种子字节串——`--show-files` 输出里的 `Magnet URI:` 行就来自前者。

**--show-files 的输出侧**。`showTorrentFile`（[src/Context.cc:L89-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L89-L95)）是 `-S` 的入口——造一个**空 Option**（`make_shared<Option>()`，所以 `PREF_DIR` 为空、路径按相对路径显示）和空 `DownloadContext`，调 `bittorrent::load` 后交给 `bittorrent::print` 模板（[src/bittorrent_helper.h:L341-L394](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L341-L394)）逐行打印 Comment/Creation Date/Created By/Mode/Announce（每层一行、层内空格分隔）/Info Hash/Piece Length/Piece 数/Total Length/URL List/Nodes/Name/Magnet URI，最后 `util::toStream` 按 `idx|path/length` 表格列出文件（格式见 [src/util.h:L366-L384](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.h#L366-L384)）。`Context` 构造函数里 `-S` 与正常下载互斥、打印后直接 `return`（[src/Context.cc:L249-L258](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L249-L258)），所以 `-S` 不会创建任何 RequestGroup。

#### 4.4.4 代码实践：验证 torrent2Magnet

1. **实践目标**：确认 `--show-files` 输出的 Magnet URI 行确实由 `torrent2Magnet` 的规则拼出。
2. **操作步骤**：
   - 运行 `./src/aria2c -S test/test.torrent`，抄下 `Magnet URI:` 一行。
   - 对照 [src/bittorrent_helper.cc:L993-L1014](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L993-L1014) 逐段核对：`xt=urn:btih:` 后应是大写十六进制 infohash；`&dn=aria2-test`；三个 `&tr=` 依三层 announce 顺序出现，URL 中的 `:` 与 `/` 被百分号转义。
   - 再用一个真实种子重复一次，观察 `tr` 数量与层数的关系。
3. **需要观察的现象**：magnet 里的 `xt` 与 `Info Hash:` 行内容相同（仅大小写差异——`Info Hash` 行是小写 `util::toHex`，`xt` 是 `toUpper` 后的大写）。
4. **预期结果**：`magnet:?xt=urn:btih:248D0A1CD08284299DE78D5C1ED359BB46717D8C&dn=aria2-test&tr=http%3A%2F%2Ftracker1&tr=...`（确切转义细节待本地验证）。把这条 magnet 交给 `aria2c` 下载（u6-l9 的流程），`parseMagnet` 应能解出与原种子相同的 infohash——属性结构在两个方向间闭环。

#### 4.4.5 小练习与答案

**练习 1**：`TorrentAttribute::infoHash` 里存的是十六进制字符串还是原始字节？怎么看出 来？

答案：原始 20 字节。字段注释 "raw hash value 20 bytes"（[src/TorrentAttribute.h:L53-L54](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h#L53-L54)），且 `getInfoHashString` 需要再过一次 `util::toHex` 才得到 40 字符文本。DHT 与 tracker 报文里携带的也是原始字节形式。

**练习 2**：`getTorrentAttrs` 的头文件注释为什么强调「不要把返回值收进 `std::shared_ptr`」？

答案：它返回的是属性槽内 `shared_ptr<ContextAttribute>` 的**裸指针**（`.get()` 后 `static_cast`），所有权仍在 `DownloadContext`。若用另一个 `shared_ptr<TorrentAttribute>` 接住返回值，两处 `shared_ptr` 各自计数，先析构一方就会把对象释放，另一方悬空。

**练习 3**：magnet 解析得到的 `TorrentAttribute` 里 `metadataSize` 是多少？为什么？

答案：0（构造函数零初始化，`parseMagnet` 不填它）。magnet 只携带 infohash，info 字典根本不存在；`metadataSize` 要等 u6-l9 的 ut_metadata 扩展把元数据收齐、走完 `processRootDictionary` 后才有值。

## 5. 综合实践：给 test.torrent 做一次「全息体检」

把本讲四个模块串起来，完成一次从字节到运行时结构的完整验证（需要一个可编译的 aria2 环境，见 u1-l2）。

**任务**：对仓库自带的 `test/test.torrent` 完成以下三步，并写一份对照笔记。

1. **读字节**（4.1）：用 4.1.4 的解码脚本（或手工）解出根字典的 7 个键：`announce`、`announce-list`、`private`、`comment`、`creation date`、`created by`、`info`（`info` 内还有 `files/name/piece length/pieces` 四个键）。
2. **看输出**（4.2、4.4）：

   ```bash
   ./src/aria2c -S test/test.torrent
   ```

   把输出的每一行与 `bittorrent::print`（[src/bittorrent_helper.h:L341-L394](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L341-L394)）里的 `printf` 逐一对应，回答：
   - `Mode:` 为什么是 `multi`？（info 里存在 `files` 列表）
   - `Announce:` 为什么是三行、每行一个 URL，而不是一行三个？（announce-list 是三个单元素 tier；print 逐层换行）
   - `Info Hash:` 为什么不是种子里「某个字段」的值？（它是重编码 info 字典后 SHA-1 的计算结果，种子里没有这个字段）
   - 文件表里两个文件的路径前缀 `aria2-test/` 从哪来？（extractFileEntries 把 `name` 插入 `pathelem[0]`）
3. **改一字节，验 infohash**（4.2 的核心结论）：

   ```bash
   cp test/test.torrent /tmp/t2.torrent
   printf 'X' | dd of=/tmp/t2.torrent bs=1 seek=<info字典内任一偏移> conv=notrunc
   ./src/aria2c -S /tmp/t2.torrent
   ```

   在 `/tmp` 的副本上（**不要改动仓库文件**）把 info 字典里的任意一个字节改掉（比如 `name` 里的一个字符），对比两次 `-S` 输出：`Info Hash:` 变了，`Magnet URI:` 的 `xt` 同步变化；而只改 info 之外的 `comment` 则 infohash 不变。这直接印证 \(\text{infoHash} = \mathrm{SHA1}(\mathrm{bencode}(\text{info}))\) 的边界——**哈希只覆盖 info 子字典**。

**预期结果**：改 info 内字节 → infohash 变；改 comment 字节 → infohash 不变。若把 `pieces` 改短导致哈希数不再等于 \(\lceil 384/128 \rceil = 3\)，aria2 会直接报 "Too few/many piece hash." 拒绝解析。全部现象待本地验证。

## 6. 本讲小结

- `.torrent` 是 bencode 编码的字典；`bencode2::decode` 委托 `GenericParser` 骨架增量解析成 `ValueBase` 树，`encode` 用 visitor 重编码，`Dict` 的 `std::map` 底座保证键有序、重编码与规范种子原文一致。
- `processRootDictionary` 是总装车间：infohash = SHA-1(重编码的 info 字典)，`pieces` 按 20 字节切成 piece 哈希表写入 `DownloadContext`，文件列表变成带累加 offset 的 `FileEntry`（多文件模式再设 basePath），最后用 \(\lceil\)总长/piece 长\(\rceil\) 校验哈希个数，全部通过才把 `TorrentAttribute` 挂上 `CTX_ATTR_BT` 槽。
- 防御贯穿解析全程：根/info 字典缺失、负长度、目录穿越、piece 哈希数不符都会抛 `BITTORRENT_PARSE_ERROR`；空 tier、非法端口节点、非字符串路径元素则被静默跳过或报错。
- `AnnounceList`/`AnnounceTier` 用「两层迭代器 + 成功置顶归位、失败层内右移层尽换层」的移动规则表达 tracker 优先级，没有数值权重；tier 自带 started→downloading/seeding/halted 事件状态机，stopped/completed 事件只发给状态允许的层。
- `TorrentAttribute` 是 BT 专属信息的纯数据聚合（infohash、metadata、announceList、nodes、private 等 11 个字段）；magnet 路径只填 infoHash/name/announceList 三项，其余等元数据收齐后补全，为 u6-l9 埋下伏笔。
- `--show-files`（`-S`）是本讲最好的实验台：`showTorrentFile` → `load` → `print`，每一行输出都能在源码里找到对应的 `printf`。

## 7. 下一步学习建议

本讲结束于「静态元数据就绪」。下一讲 **u6-l2 BtRegistry 与 BtSetup** 讲这些元数据如何驱动 BT 运行时对象的装配（PeerStorage、PieceStorage、BtInteractive 等十余个对象如何在下载开始时挂到注册表）。后续衔接：

- 想看 infohash 在网络协议里怎么用：u6-l3（握手消息里交换 infohash 校验身份）、u6-l7（tracker 请求的 `info_hash` 参数）、u7（DHT 以 infohash 为键存 peer）。
- 想看 `metadata`/`metadataSize` 的真正用途：u6-l9 magnet 与 ut_metadata。
- 想深入解析器骨架（`StructParserStateMachine`、增量状态机）：u9-l2 ValueBase 与解析器骨架。
- 想验证理解最直接的方式：给 `test/BittorrentHelperTest.cc` 仿照 `testLoadFromMemory` 补一个用例（例如断言 `privateTorrent == true`），跑 `make check`（测试体系见 u9-l5）。
