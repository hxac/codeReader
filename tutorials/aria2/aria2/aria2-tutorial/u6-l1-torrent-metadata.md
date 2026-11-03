# u6-l1 torrent 元数据解析：bencode 与种子结构

## 1. 本讲目标

本讲是 BitTorrent 单元（单元 6）的第一讲，回答一个总问题：**一个 `.torrent` 文件里的字节，是如何变成 aria2 能理解的下载任务的？**

学完本讲你应该能够：

1. 徒手读懂 bencode 编码（.torrent 文件的本体格式），并说出 `bencode2` 模块的 decode/encode 两条链路。
2. 跟踪 `bittorrent_helper::load` → `processRootDictionary` 的完整解析流程，说出 torrent 字典每个关键字段（`info`、`infohash`、`piece length`、`pieces`、`files`、`name`、`announce`/`announce-list`、`nodes`）的读取位置与校验规则。
3. 解释 `TorrentAttribute` 承载哪些 BT 专属运行时信息，以及它与 `DownloadContext` 公共字段的分界。
4. 理解 `AnnounceList`/`AnnounceTier` 如何组织多 tracker 层级（tier），以及 announce 成功/失败时层级如何轮换。

## 2. 前置知识

### 2.1 BitTorrent 与 .torrent 文件

BitTorrent 是一种 P2P 文件分发协议：你不再只从服务器下载，而是同时从其他下载者（peer）处获取数据。`.torrent` 文件（也叫 metainfo 文件）就是一份"任务说明书"，它回答三件事：

- **下什么**：文件名、文件列表、每个文件多大；
- **怎么校验**：数据被切成等长的 piece，每个 piece 有一个 SHA-1 哈希；
- **去哪找同伴**：tracker 服务器列表（以及可选的 DHT 引导节点）。

### 2.2 bencode 语法速成

.torrent 文件的全部内容就是一个 bencode 编码的字典。bencode 只有四种类型，语法极简：

| 类型 | 语法 | 例子 | 含义 |
|------|------|------|------|
| 字符串 | `<十进制长度>:<原始字节>` | `4:spam` | 字符串 "spam" |
| 整数 | `i<十进制>e` | `i128e` | 整数 128 |
| 列表 | `l<元素...>e` | `l4:spami1ee` | ["spam", 1] |
| 字典 | `d<键值对...>e` | `d3:bar4:spam3:fooi42ee` | {"bar":"spam","foo":42} |

字典的键必须是字符串，且规范要求**按键名排序**存放。注意长度前缀说的是**字节数**，不是字符数，所以 bencode 天然是二进制安全的。

### 2.3 ValueBase：内存中的动态类型树

C++ 没有原生"字典/列表"这种动态类型，aria2 用 `ValueBase` 家族（`String`/`Integer`/`Bool`/`Null`/`List`/`Dict`）在内存里表示解码后的 bencode/JSON 树。其中 `Dict` 底层是 `std::map`（[src/ValueBase.h:247-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L247-L249)），键始终有序——这一点对本讲的 infohash 计算至关重要。ValueBase 的深入剖析在 u9-l2，本讲只需把它当作"已经解析好的树"来用。

### 2.4 承接前讲

- u3-l3 讲过：命令行给出 .torrent 文件后，`Context` 构造函数走 `createRequestGroupForBitTorrent` 分支创建 `RequestGroup`。本讲下潜这个分支内部的第一步——解析。
- u3-l2 讲过：`DownloadContext` 是任务元数据聚合根，`ContextAttribute` 以"空基类加枚举槽位"的方式挂载协议专属信息。本讲的 `TorrentAttribute` 正是 BT 槽位（`CTX_ATTR_BT`）上的对象。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/bencode2.cc` | bencode 编解码门面：`decode` 把字节流转成 ValueBase 树，`encode` 反向 |
| `src/GenericParser.h` | 通用"流式解析文件"模板 `parseFile`：4KB 缓冲分块喂给解析器 |
| `src/bittorrent_helper.cc` | 本讲主角：`load`/`loadFromMemory` → `processRootDictionary` 及一族 `extract*` 函数 |
| `src/bittorrent_helper.h` | 上述函数声明；`print()` 模板实现 `--show-files` 的控制台输出 |
| `src/TorrentAttribute.h/.cc` | BT 专属属性的纯数据结构定义 |
| `src/AnnounceList.h/.cc` | 运行期多 tracker 层级游标：轮换、事件判定 |
| `src/AnnounceTier.h/.cc` | 单个 tier：URL 队列 + announce 事件状态机 |
| `src/download_helper.cc` | 调用方：种子文件如何被读入并转成 RequestGroup |
| `src/Context.cc` | `--show-files` 的入口分支与 `showTorrentFile` |
| `src/DefaultBtAnnounce.cc` | 运行期消费方：从 `TorrentAttribute::announceList` 构造 `AnnounceList` |
| `test/AnnounceListTest.cc` | tier 轮换行为的权威用例，本讲实践材料 |
| `test/*.torrent` | 仓库自带的迷你种子文件（test.torrent、single.torrent 等），可直接用于实践 |

## 4. 核心概念与源码讲解

### 4.1 bencode2：bencode 的编解码门面

#### 4.1.1 概念说明

`bencode2` 是一个非常薄的命名空间（不足 130 行），只提供两个能力：

- **decode**：把 bencode 字节流解析成 `ValueBase` 树。真正的逐字节状态机在 `BencodeParser`/`ValueBaseBencodeParser` 里（u9-l2 详讲），`bencode2::decode` 只是"一次性喂完所有字节并取结果"的便捷封装。
- **encode**：把 `ValueBase` 树重新序列化回 bencode 文本。

为什么需要 encode？因为 **infohash 的定义是 SHA-1( info 字典的 bencode 编码 )**。aria2 解析种子时拿到的是树，想算 infohash 就必须把 `info` 子树重新编码成字节串。

#### 4.1.2 核心流程

decode 链路：

```text
调用方
  └─ bencode2::decode(data, len)
       └─ ValueBaseBencodeParser::parseFinal(data, len, error)   # 逐字节状态机
            ├─ error < 0 → 抛 DL_ABORT_EX2(BENCODE_PARSE_ERROR=25)
            └─ 成功 → 返回 ValueBase 树（String/Integer/List/Dict）
```

encode 链路（访问者模式）：

```text
bencode2::encode(vlb)
  └─ BencodeValueBaseVisitor 遍历 ValueBase 树
       ├─ String  → 写 "<size>:<bytes>"
       ├─ Integer → 写 "i<value>e"
       ├─ List    → 写 "l" + 递归各元素 + "e"
       └─ Dict    → 写 "d" + 按 std::map 有序逐对写 <键><值> + "e"
```

关键保证：`Dict` 用 `std::map` 存键，遍历天然按字典序输出，所以 `encode(decode(x))` 对规范种子是**字节级还原**的——infohash 因此不会因为"重新编码"而算错。

#### 4.1.3 源码精读

三参版 `decode` 是核心，两个短重载只是转发（[src/bencode2.cc:50-61](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L50-L61)）：

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
  ...
}
```

[src/bencode2.cc:63-76](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L63-L76)：把整块内存交给 `parseFinal`；`error < 0` 表示语法错误，抛出错误码 25（`BENCODE_PARSE_ERROR`，见 [src/error_code.h:71](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L71)）的异常。`end` 出参返回消费的字节数，供"一个 buffer 里有多段 bencode"的场景使用。

encode 侧是一个局部 Visitor 类，对 String/Integer/List/Dict 各有一个 `visit`（[src/bencode2.cc:78-126](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L78-L126)）。String 的写法最能体现 bencode 风格（[src/bencode2.cc:85-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L85-L90)）：

```cpp
virtual void visit(const String& string) CXX11_OVERRIDE
{
  const std::string& s = string.s();
  out_ << s.size() << ":";
  out_.write(s.data(), s.size());
}
```

先写十进制长度、冒号，再原样写字节。Dict 的 `visit` 直接 `for (const auto& e : dict)` 顺序遍历 `std::map`，键有序因此无需额外排序（[src/bencode2.cc:109-119](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L109-L119)）。

从磁盘读种子文件时，aria2 不把整个文件一次性读进内存，而是用 `GenericParser.h` 的 `parseFile` 模板分块喂数据（[src/GenericParser.h:91-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/GenericParser.h#L91-L115)）：

```cpp
std::array<char, 4_k> buf;
while ((nread = read(fd, buf.data(), buf.size())) > 0) {
  nproc = parser.parseUpdate(buf.data(), nread);
  ...
}
return parser.parseFinal(0, 0, nproc);
```

每轮最多读 4KB，交给 `parseUpdate` 增量解析，最后 `parseFinal(0,0,...)` 收尾。这种"推一块、消化一块"的流式设计与 u4-l2 的 `HttpHeaderProcessor` 一脉相承——解析进度全部外置于状态机，天然免疫文件任意切分。

#### 4.1.4 代码实践：徒手解码一个真种子

**实践目标**：不借助工具（除了 `xxd`/`cat -v`），人工读出仓库自带迷你种子的结构。

**操作步骤**：

1. 查看单文件种子的原始字节：

   ```bash
   cat -v test/single.torrent
   ```

2. 对照 2.2 节的语法表，从第一个字符 `d` 开始逐 token 标注。它的开头是：

   ```text
   d8:announce37:http://aria.rednoah.com/announce.php 7:comment...
     17:REDNOAH.COM RULES13:creation datei1123456789e...
     4:infod4:name19:aria2-0.8.2.tar.bz26:lengthi384e
     12:piece lengthi128e6:pieces60:AAAA...eeee
   ```

   注意每个长度前缀都要数一遍字节，例如 `8:announce` 中的 `announce` 恰好 8 个字符。

3. 用 `aria2c` 的 `-S`（`--show-files`）交叉验证你的解读：

   ```bash
   ./src/aria2c -S test/single.torrent
   ```

**需要观察的现象**：`pieces` 字段的长度前缀是 `60:`，即 60 字节的哈希串；而 `piece length` 是 `i128e`、`length` 是 `i384e`。

**预期结果**：\( 60 = 3 \times 20 \)，即 3 个 piece、每 piece 哈希 20 字节（SHA-1 摘要长度）；又 \( \lceil 384 / 128 \rceil = 3 \)，两者吻合。`-S` 输出应显示 `Mode: single`、总长 384B、`The Number of Pieces: 3`。具体输出格式待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：bencode 里 `i-3e`、`4:12:e`、`le`、`de` 各是什么？
**答案**：整数 -3；字符串 `"12:e"`（长度 4，内容就是字符 `1`、`2`、`:`、`e`——冒号后不再有特殊含义）；空列表；空字典。

**练习 2**：`bencode2::encode` 为什么能保证字典键按规范排序输出？
**答案**：因为 `ValueBase::Dict` 底层是 `std::map<std::string, ...>`（[src/ValueBase.h:247-249](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ValueBase.h#L247-L249)），`std::map` 按键升序遍历，encode 只是照着遍历顺序写，无需额外排序。

### 4.2 bittorrent_helper：从种子字典到 DownloadContext

#### 4.2.1 概念说明

`bittorrent_helper.cc` 是 BT 子系统的"总装车间"之一，本模块关注它的解析职责。torrent 根字典的键很多，源码把每个键的读取拆成独立小函数，总调度是 `processRootDictionary`。所有键名以常量集中在文件头部（[src/bittorrent_helper.cc:71-88](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L71-L88)）：`info`、`pieces`、`piece length`、`files`、`length`、`name`/`name.utf-8`、`path`/`path.utf-8`、`private`、`url-list`、`announce`、`announce-list`、`nodes`、`creation date`、`comment`、`created by`。

入口是一族 `load*` 函数，区别只在"种子内容从哪来"：

- `load(torrentFile, ...)`：从磁盘文件读（`-S` 展示走这条路）；
- `loadFromMemory(...)`：从内存字节或**已解析好的 ValueBase 树**装配（正常下载走这条路——调用方 `download_helper` 已先解析过一次文件以提取 infohash）。

#### 4.2.2 核心流程

`processRootDictionary` 的执行顺序（[src/bittorrent_helper.cc:412-529](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L412-L529)）：

```text
1. 校验根节点是 Dict，取 info 子字典（缺则抛 BITTORRENT_PARSE_ERROR）
2. infohash = SHA-1(bencode2::encode(info 字典))；同时保存 metadata（info 原文）与 metadataSize
3. 读 pieces 字符串 → numPieces = len(pieces) / 20
4. 读 piece length（缺失或为负 → 抛错）；写入 ctx->setPieceLength
5. extractPieceHash：把 pieces 字符串按 20 字节切段 → ctx 的分块哈希表
6. 读 private 标志（i1e → 私有种子）
7. extractUrlList：读 url-list（HTTP web-seed），与外部传入 uris 合并、排序去重
8. extractFileEntries：读 name + files/length → FileEntry 列表 → ctx
   └─ 一致性检查：⌈总长/pieceLength⌉ == numPieces，否则抛 "Too few/many piece hash."
9. extractAnnounce / extractNodes → tracker 层级 / DHT 节点
10. 读 creation date / comment / created by
11. ctx->setAttribute(CTX_ATTR_BT, torrent)   ← TorrentAttribute 挂载点
```

piece 数一致性公式：

\[ N_{piece} = \left\lceil \frac{L_{total}}{L_{piece}} \right\rceil \]

#### 4.2.3 源码精读

**入口函数**。`load` 用 `ValueBaseBencodeParser` + `parseFile` 流式读文件后交给 `processRootDictionary`（[src/bittorrent_helper.cc:531-539](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L531-L539)）：

```cpp
void load(const std::string& torrentFile, ...)
{
  ValueBaseBencodeParser parser;
  processRootDictionary(ctx, parseFile(parser, torrentFile).get(), option,
                        torrentFile, overrideName, std::vector<std::string>());
}
```

带 `uris` 参数的重载（[src/bittorrent_helper.cc:541-549](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L541-L549)）把命令行附加 URI 并入 web-seed 列表。`loadFromMemory` 家族（[src/bittorrent_helper.cc:551-601](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L551-L601)）有五个重载，分别接受原始字节、`std::string` 或现成的 `ValueBase*`，最终全部汇入同一个 `processRootDictionary`——**无论种子来自文件、内存还是 magnet 元数据，装配逻辑只有一份**。

**infohash 计算**是整个流程最关键的三行（[src/bittorrent_helper.cc:432-440](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L432-L440)）：

```cpp
std::string encodedInfoDict = bencode2::encode(infoDict);
unsigned char infoHash[INFO_HASH_LENGTH];
message_digest::digest(infoHash, INFO_HASH_LENGTH,
                       MessageDigest::sha1().get(), encodedInfoDict.data(),
                       encodedInfoDict.size());
torrent->infoHash.assign(&infoHash[0], &infoHash[INFO_HASH_LENGTH]);
torrent->metadata = encodedInfoDict;
```

注意 aria2 **不是**从原文件里"截取" info 的原始字节，而是把解析出的 `info` 子树重新 encode 再哈希。由于 `Dict` 键有序，规范种子下两者字节相同。`INFO_HASH_LENGTH` 为 20（[src/BtConstants.h:43](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtConstants.h#L43)）。重新编码的 `info` 原文同时存进 `torrent->metadata`，这是 magnet 下载里 ut_metadata 扩展向其他 peer 分发元数据的原料（u6-l9 详讲）。

**pieces 与 piece length**（[src/bittorrent_helper.cc:442-474](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L442-L474)）：`pieces` 是缺失即抛错的可选性最低字段；piece 数就是字节数除以 20。哈希切分由 `extractPieceHash` 完成（[src/bittorrent_helper.cc:99-110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L99-L110)），把连续字节按 20 字节切片后写入 `DownloadContext` 的分块哈希表——这一步正是 u5-l5 分块校验的数据来源。

**文件条目 `extractFileEntries`**（[src/bittorrent_helper.cc:177-348](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L177-L348)）分两条路：

- 名字解析顺序：`overrideName`（调用方指定，如 RPC 参数）＞ `name.utf-8` ＞ `name` ＞ 兜底 `<种子文件名>.file`（[src/bittorrent_helper.cc:185-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L185-L211)）。`*.utf-8` 后缀键是 BEP-47 为非 ASCII 文件名准备的，优先级更高。
- **多文件模式**：info 里存在 `files` 列表时，每个文件条目是含 `length` 与 `path`（路径按目录层级拆成字符串列表）的字典；代码累加 `offset` 把各文件铺成一条逻辑偏移空间（[src/bittorrent_helper.cc:214-299](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L214-L299)）。这正是 u3-l2 讲过的"多文件首尾相接"的诞生地。
- **单文件模式**：长度直接取 info 顶层的 `length`（[src/bittorrent_helper.cc:301-341](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L301-L341)）。
- 两种模式都做**目录穿越防护**：`util::detectDirTraversal` 一旦发现 `..` 之类的路径成分立即抛 `BITTORRENT_PARSE_ERROR`（[src/bittorrent_helper.cc:197-201](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L197-L201)、[src/bittorrent_helper.cc:276-279](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L276-L279)）——恶意种子里一个 `path: ["../../etc"]` 就能让你把文件写到任意目录，所以这是安全底线。

**piece 数一致性检查**（[src/bittorrent_helper.cc:498-501](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L498-L501)）：

```cpp
if ((ctx->getTotalLength() + pieceLength - 1) / pieceLength != numPieces) {
  throw DL_ABORT_EX2("Too few/many piece hash.", error_code::BITTORRENT_PARSE_ERROR);
}
```

`(a + b - 1) / b` 是整数上取整的惯用写法。种子声称的文件总长必须与哈希条数自洽，否则拒绝。

**announce 与 nodes**。`extractAnnounce`（[src/bittorrent_helper.cc:350-381](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L350-L381)）优先读 `announce-list`（BEP-12 多 tracker），每个 tier 是一个 URL 列表；只有种子没有 `announce-list` 时才退回单值 `announce`。注意它对每个 URL 做了 `util::strip`——本讲实践用的 `test/test.torrent` 里 tracker URL 末尾故意带空格，解析时会被剪掉。`extractNodes`（[src/bittorrent_helper.cc:383-410](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L383-L410)）读 `nodes` 列表（`[["hostname", port], ...]`），这是 DHT 自举节点（BEP-5），端口范围 1~65535 之外静默跳过。

**校验规则汇总**：

| 校验点 | 位置 | 错误信息 | 错误码 |
|--------|------|----------|--------|
| 根节点不是字典 | [src/bittorrent_helper.cc:420-424](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L420-L424) | "torrent file does not contain a root dictionary." | 26 |
| 缺 info / pieces / piece length / length | 同上函数各分支 | "Missing %s in torrent metainfo."（[src/message.h:153](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message.h#L153)） | 26 |
| 长度字段为负 | [src/bittorrent_helper.cc:233-237](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L233-L237)、[src/bittorrent_helper.cc:465-469](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L465-L469) | "Invalid %s ..." | 26 |
| 目录穿越 | [src/bittorrent_helper.cc:197-201](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L197-L201) | MSG_DIR_TRAVERSAL_DETECTED | 26 |
| piece 数与总长不符 | [src/bittorrent_helper.cc:498-501](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L498-L501) | "Too few/many piece hash." | 26 |
| bencode 语法错误 | [src/bencode2.cc:69-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bencode2.cc#L69-L73) | "Bencode decoding failed" | 25 |

**调用方视角**（衔接 u3-l3）：正常下载路径上，`createRequestGroupForBitTorrent` 先 `parseFile` 得到 ValueBase 树，校验非空后交给 `createBtRequestGroup`，后者调用 `loadFromMemory(ValueBase*, ...)`（[src/download_helper.cc:305-326](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L305-L326)、[src/download_helper.cc:185-232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L185-L232)）。解析出的 infohash 在任务启动时还要过一道"重复注册"检查（[src/RequestGroup.cc:280-295](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L280-L295)）——同一 infohash 不允许注册两个任务。

#### 4.2.4 代码实践：构造损坏的种子，逐一触发校验分支

**实践目标**：亲眼看到上表中的错误信息从源码里"长出来"，把错误信息和抛出点一一对应。

**操作步骤**：

1. 写一个小脚本（示例代码，放在仓库外任意目录即可），从 `test/single.torrent` 派生三个损坏变体：

   ```python
   # 示例代码：make_broken_torrents.py
   data = open("test/single.torrent", "rb").read()

   # 变体1：整个删掉 "12:piece lengthi128e" 这一段（21 字节）
   open("no_piece_length.torrent", "wb").write(
       data.replace(b"12:piece lengthi128e", b""))
   # 变体2：pieces 从 60 字节砍到 40 字节（去掉最后一个 piece 哈希）
   open("short_pieces.torrent", "wb").write(
       data.replace(b"6:pieces60:", b"6:pieces40:")[:-20])
   # 变体3：把 "4:info" 改名成 "4:infx"，让 info 键消失
   open("no_info.torrent", "wb").write(
       data.replace(b"4:infod", b"4:infxd"))
   ```

   由于 bencode 的每个元素都是自定界的，删掉一个完整的键值对后剩余部分仍是合法 bencode，所以这些变体能走到"字段级校验"而不是语法错误。

2. 依次运行：

   ```bash
   ./src/aria2c -S no_piece_length.torrent
   ./src/aria2c -S short_pieces.torrent
   ./src/aria2c -S no_info.torrent
   ```

**需要观察的现象**：三条命令分别报出不同的错误消息。

**预期结果**：变体 1 报 "Missing piece length in torrent metainfo."（[src/bittorrent_helper.cc:460-463](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L460-L463)）；变体 2 报 "Too few/many piece hash."（总长 384 / 128 向上取整是 3，但 pieces 只剩 2 个哈希）；变体 3 报 "Missing info in torrent metainfo."。`-S` 模式下异常被 `showFiles` 的 try/catch 捕获并打印堆栈（[src/Context.cc:140-142](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L140-L142)），进程最终退出码是否精确等于 26 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：一个种子总长 1000 字节、piece length 256，`pieces` 字段应当是多少字节？
**答案**：\( \lceil 1000/256 \rceil = 4 \) 个 piece，每 piece 哈希 20 字节，共 80 字节。若种子给了 100 字节（5 个哈希），就会命中 "Too few/many piece hash."。

**练习 2**：为什么 aria2 用"重新编码 info 字典"而不是"从原文件截取 info 字节"来算 infohash？两种方式什么时候结果不同？
**答案**：解析后手里只有 ValueBase 树，树没有"在原文件中的字节区间"信息，重新编码是最自然的做法。当且仅当原种子的 info 字典键未按字典序排列（违反 bencode 规范的病态种子）时，重编码字节与原文不同，算出的 infohash 也会与其他客户端不一致。规范种子下两者等价。

**练习 3**：`-S` 展示与正常下载分别调用哪个 `load*` 入口？
**答案**：`-S` 走 `bittorrent::load(文件)`（[src/Context.cc:89-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L89-L95)）；正常下载由 `download_helper` 先 `parseFile` 再走 `loadFromMemory(ValueBase*, ...)`（[src/download_helper.cc:196-197](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L196-L197)）。

### 4.3 TorrentAttribute：BT 专属信息的挂载点

#### 4.3.1 概念说明

解析产物分成两半：

- **协议无关的一半**写进 `DownloadContext` 公共字段：`FileEntry` 列表（文件路径、长度、偏移、web-seed URI）、`pieceLength`、分块哈希表——HTTP/FTP 下载同样用这些字段，所以 BT 信息必须"降维"成公共形态才能复用 u5 的存储与校验机制。
- **BT 专属的一半**装进 `TorrentAttribute`，经 `ctx->setAttribute(CTX_ATTR_BT, ...)` 挂到 `DownloadContext` 的 BT 属性槽上（u3-l2 讲过的"空基类加枚举槽位"机制，目前只有 BT 一个槽）。

这个纯数据结构没有任何行为，字段定义一目了然（[src/TorrentAttribute.h:48-69](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.h#L48-L69)）：

| 字段 | 类型 | 含义 | 填充者 |
|------|------|------|--------|
| `name` | string | 种子名（多文件模式的目录名） | extractFileEntries |
| `mode` | BtFileMode | single / multi / none | extractFileEntries |
| `announceList` | vector<vector<string>> | tracker 层级（tier × URL） | extractAnnounce / parseMagnet |
| `nodes` | vector<pair<string,u16>> | DHT 自举节点 | extractNodes |
| `infoHash` | string | 20 字节原始 SHA-1（非十六进制文本） | processRootDictionary |
| `metadata` / `metadataSize` | string / size_t | info 字典的 bencode 原文及其长度 | processRootDictionary |
| `privateTorrent` | bool | 私有种子标志（禁 DHT/PEX） | processRootDictionary |
| `creationDate` / `comment` / `createdBy` | time_t / string / string | 三项展示性元信息 | processRootDictionary |
| `urlList` | vector<string> | web-seed 种子服务器列表 | extractUrlList |

构造函数只做零值初始化（[src/TorrentAttribute.cc:39-45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/TorrentAttribute.cc#L39-L45)）。

#### 4.3.2 核心流程

属性的生命周期：

```text
解析期：processRootDictionary 填充 TorrentAttribute → setAttribute(CTX_ATTR_BT)
读取期：getTorrentAttrs(dctx) 取回裸指针
        getInfoHashString(dctx) = util::toHex(infoHash)   ← 供日志与重复检查
运行期：BtSetup 等模块按需读取（u6-l2 详讲）
派生期：torrent2Magnet(attrs) 把属性反向拼成 magnet URI
```

一个特殊入口是 **magnet**：`parseMagnet`（[src/bittorrent_helper.cc:901-960](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L901-L960)）不读 .torrent 文件，而是从 `magnet:?xt=urn:btih:...` 里解出 infohash（支持 40 位十六进制与 32 位 Base32 两种形态）、`dn`（名字）与 `tr`（tracker），产出一个**只有 infohash/name/announceList 三项有值的极简 TorrentAttribute**（[src/bittorrent_helper.cc:962-966](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L962-L966) 的 `loadMagnet` 直接挂槽）。此时 `metadata` 为空，任务处于"元数据获取模式"，这是 u6-l9 的主线，本讲只需记住这个极简形态。

#### 4.3.3 源码精读

挂载与读取的对称代码。挂载在 `processRootDictionary` 的最后一行（[src/bittorrent_helper.cc:527](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L527)）：

```cpp
ctx->setAttribute(CTX_ATTR_BT, std::move(torrent));
```

读取的一对便捷函数（[src/bittorrent_helper.cc:603-611](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L603-L611)）：

```cpp
TorrentAttribute* getTorrentAttrs(DownloadContext* dctx)
{
  return static_cast<TorrentAttribute*>(dctx->getAttribute(CTX_ATTR_BT).get());
}
```

注意头文件里特意注释"不要把返回值接进 `std::shared_ptr`"（[src/bittorrent_helper.h:157-159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L157-L159)）——返回的是共享指针内部的裸指针，生命周期归 `DownloadContext` 所有。`getInfoHashString` 是最常用的读取口，把 20 字节原始哈希转成 40 字符十六进制（[src/bittorrent_helper.cc:629-632](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L629-L632)）。

`torrent2Magnet` 展示了属性的"反向序列化"：xt 取大写十六进制 infohash、dn 取名字、每个 tier 的每个 URL 都展开成一个 `tr=` 参数（[src/bittorrent_helper.cc:993-1014](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L993-L1014)）。`-S` 输出的最后一行 `Magnet URI:` 就来自它。

`--show-files` 的输出则把 TorrentAttribute 与公共字段一起打印，模板函数 `print` 位于 [src/bittorrent_helper.h:341-394](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L341-L394)：

```cpp
o.printf("Info Hash: %s\n", util::toHex(torrentAttrs->infoHash).c_str());
o.printf("Piece Length: %sB\n", util::abbrevSize(dctx->getPieceLength()).c_str());
```

[src/bittorrent_helper.h:367-371](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.h#L367-L371)：这两行正好一属性一公共字段，是"分界"最直观的注脚。`-S` 的完整触发链：`Context` 构造函数发现 `PREF_SHOW_FILES` 为真 → `showTorrentFile` → `bittorrent::load` + `print`（[src/Context.cc:89-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L89-L95)、[src/Context.cc:249-253](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L249-L253)）；若参数是普通 URI 则先经 `ProtocolDetector` 嗅探文件类型再分派（[src/Context.cc:113-144](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Context.cc#L113-L144)）。

#### 4.3.4 代码实践：`-S` 输出与字段的一一对照

**实践目标**：把 `aria2c -S` 的每一行输出钉到 `TorrentAttribute` 的具体字段或 `DownloadContext` 的公共字段上，固化"分界"认知。

**操作步骤**：

1. 运行三个仓库自带种子，观察输出差异：

   ```bash
   ./src/aria2c -S test/test.torrent        # 多文件 + announce-list + comment
   ./src/aria2c -S test/single.torrent      # 单文件，只有单值 announce
   ./src/aria2c -S test/url-list-singleFile.torrent   # 带 web-seed url-list
   ```

2. 对照下表填写来源（答案列已给出，先自己填再核对）：

| `-S` 输出行 | 来源字段 |
|-------------|----------|
| Comment: | `TorrentAttribute::comment` |
| Creation Date: | `TorrentAttribute::creationDate` |
| Created By: | `TorrentAttribute::createdBy` |
| Mode: | `TorrentAttribute::mode`（经 getModeString，[src/bittorrent_helper.cc:1084-1094](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1084-L1094)） |
| Announce: | `TorrentAttribute::announceList`（每行一个 tier） |
| Info Hash: | `TorrentAttribute::infoHash` 的十六进制 |
| Piece Length: | `DownloadContext` 公共字段 |
| The Number of Pieces: | 公共字段（分块哈希表长度） |
| Total Length: | 公共字段（FileEntry 总长） |
| URL List: | `TorrentAttribute::urlList` |
| Nodes: | `TorrentAttribute::nodes` |
| Name: | `TorrentAttribute::name` |
| Magnet URI: | 由 `torrent2Magnet(attrs)` 现算 |
| 文件清单 | `DownloadContext` 的 FileEntry 列表 |

**需要观察的现象**：`test.torrent` 的 Announce 区域只列出 `http://tracker1/2/3` 三个 tier，而种子里的 `announce` 主键（`http://aria.rednoah.com/announce.php`）没有出现——因为 `announce-list` 存在时主键被忽略（4.2.3 节的优先级规则）。

**预期结果**：三个种子的 Mode 分别为 multi / single / single；`url-list-singleFile.torrent` 会多出 URL List 区域。具体每行的值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么 piece 哈希表放进 `DownloadContext` 公共字段而不是 `TorrentAttribute`？
**答案**：分块校验机制（u5-l5 的 ChunkChecksumValidator）对 HTTP（Metalink 分块哈希）和 BT 是同一套，piece 哈希是协议无关的公共数据；放进公共字段后 PieceStorage/校验器无需感知 BT。

**练习 2**：magnet 任务刚创建时，`TorrentAttribute` 里哪些字段有值？
**答案**：只有 `infoHash`、`name`（`[METADATA]` 前缀，缺 dn 时用 infohash 十六进制兜底）和 `announceList`（每个 `tr=` 一个 tier），见 [src/bittorrent_helper.cc:940-959](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L940-L959)。`metadata` 为空——这正是判断"元数据获取模式"的依据（[src/RequestGroup.cc:282](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L282)）。

### 4.4 AnnounceList 与 AnnounceTier：多 tracker 层级

#### 4.4.1 概念说明

`TorrentAttribute::announceList` 是**解析期的静态快照**（`vector<vector<string>>`），描述"这个种子有哪些 tracker"。到了运行期，tracker 交互（u6-l7）需要的是一台"游标机"：当前该向谁 announce、失败了下一个找谁、全部失败了吗、这一轮该报 started 还是 stopped 事件。这台游标机就是 `AnnounceList`，它的一个构造重载正好接受解析期的快照（[src/AnnounceList.cc:47-52](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L47-L52)）。

层级语义来自 BEP-12 多 tracker 规范：

- **tier（层）内的 tracker 互为镜像备份**：正常情况下它们跟踪同一群 peer，失败一个换同层下一个；
- **tier 之间是并列的多台 tracker**：一层全挂才降级到下一层；
- 每个 tier 附带一个**事件状态机**（`AnnounceTier::event`），记录本轮 announce 处于生命周期哪个阶段。

#### 4.4.2 核心流程

两个核心游标操作：

```text
announceSuccess() （tracker 应答成功）
  1. 当前 tier 的 event 前进一步（STARTED→DOWNLOADING 等）
  2. 把成功的 URL 从原位摘出，插到本 tier 队首   ← 下次优先用它
  3. 游标整体回到第一个 tier 的第一个 URL

announceFailure() （tracker 失败）
  1. currentTracker_ 前移一位（同 tier 换备份）
  2. 若同层用尽 → tier 的 event 强制推进（nextEventIfAfterStarted）
     → currentTier_ 降级到下一 tier 的首个 URL
  3. 若所有 tier 用尽 → currentTrackerInitialized_ = false
     → allTiersFailed() 返回 true
```

事件状态机（[src/AnnounceTier.cc:46-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.cc#L46-L64)）：

```text
STARTED ──成功──▶ DOWNLOADING（此后周期性 announce 不带事件参数）
STARTED_AFTER_COMPLETION ──▶ SEEDING
COMPLETED ──▶ SEEDING
STOPPED ──▶ HALTED
```

#### 4.4.3 源码精读

**数据成员**只有四个（[src/AnnounceList.h:50-59](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.h#L50-L59)）：tier 队列 `tiers_`、双游标 `currentTier_`/`currentTracker_`、以及有效性标志 `currentTrackerInitialized_`。tier 本体是"URL 双端队列 + 事件枚举"（[src/AnnounceTier.h:57-58](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.h#L57-L58)），构造时 event 固定为 `STARTED`（[src/AnnounceTier.cc:39-42](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.cc#L39-L42)）。

**构造**：`reconfigure` 把 `vector<vector<string>>` 逐层转成 `AnnounceTier`（空层直接丢弃），随后 `resetIterator` 把游标放到第一层第一个（[src/AnnounceList.cc:63-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L63-L95)）。

**announceSuccess**（[src/AnnounceList.cc:107-117](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L107-L117)）：

```cpp
(*currentTier_)->nextEvent();
auto url = *currentTracker_;
(*currentTier_)->urls.erase(currentTracker_);
(*currentTier_)->urls.push_front(std::move(url));
currentTier_ = std::begin(tiers_);
currentTracker_ = std::begin((*currentTier_)->urls);
```

四步对应 4.4.2 流程的 1-2-3：摘下成功 URL 插到队首，再把游标整体归位到第一层。**这解释了 aria2 的 tracker 偏好：谁最近成功过就一直用谁，直到它失败。**

**announceFailure**（[src/AnnounceList.cc:119-135](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L119-L135)）：同层耗尽才降层，降层时顺手 `nextEventIfAfterStarted()` 推进事件（只对 STOPPED/COMPLETED 生效，[src/AnnounceTier.cc:66-78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceTier.cc#L66-L78)）。

**事件的"层资格"判定**：退出/完成事件不是每层都能发。两个仿函数 `FindStoppedAllowedTier`/`FindCompletedAllowedTier` 按当前 event 判断该层是否接受 stopped/completed 事件（[src/AnnounceList.cc:174-206](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L174-L206)），配套的 `countStoppedAllowedTier`/`moveToStoppedAllowedTier` 等（[src/AnnounceList.cc:208-241](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L208-L241)）在退出前把游标 wrap-around 挪到合法层。例如刚降到第二层（event 仍是 STARTED）时要发 stopped，就得先 `moveToStoppedAllowedTier` 找一个已在 DOWNLOADING/SEEDING 状态的层。运行期消费方 `DefaultBtAnnounce` 持有 `AnnounceList announceList_`，构造自 `torrentAttrs->announceList`（[src/DefaultBtAnnounce.cc:70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L70)），并在每次 announce 前用上述 API 校准事件（[src/DefaultBtAnnounce.cc:119-146](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DefaultBtAnnounce.cc#L119-L146)）。

**解析期与运行期的桥**：`adjustAnnounceUri` 在创建 RequestGroup 时按 `--bt-exclude-tracker`/`--bt-tracker` 两个选项改造**解析期**的 `TorrentAttribute::announceList`（先删后加，每个新 URI 自成一层，`*` 通配全部删除；[src/bittorrent_helper.cc:1029-1082](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1029-L1082)），调用点在 [src/download_helper.cc:209-211](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L209-L211)。`AnnounceList::shuffle`（[src/AnnounceList.cc:243-250](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L243-L250)）则把每层 URL 随机洗牌，避免全体客户端同时压垮层内第一个 tracker。

#### 4.4.4 代码实践：用单元测试推演 tier 轮换

**实践目标**：不运行 aria2 也能准确预测 announceSuccess/Failure 对游标的影响，用测试用例验证预测。

**操作步骤**：

1. 阅读 [test/AnnounceListTest.cc:64-109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AnnounceListTest.cc#L64-L109) 的 `testSingleElementList`：bencode 串 `ll8:tracker1el8:tracker2el8:tracker3ee` 表示三个各自只有一个 URL 的 tier。
2. 先遮住断言，只看操作序列，自己在纸上推演每次 `announceFailure()`/`announceSuccess()` 后 `getAnnounce()` 的返回值与 `getEventString()`。
3. 编译并运行测试验证（测试二进制是 `test/aria2c`，会用 CppUnit 跑全部用例，从输出中过滤关心的类即可）：

   ```bash
   make -C src -j$(nproc) && make -C test check 2>&1 | grep -i announce
   ```

**需要观察的现象**：三次 `announceFailure()` 后 `allTiersFailed()` 为真；`resetTier()` 后游标回到 tracker1；随后对 tracker2 `announceSuccess()`，游标跳回 tracker1（第一层优先）。

**预期结果**：与你的手推完全一致。最值得注意的一步：游标已因两次失败停在第二层的 tracker2，此时 `announceSuccess()` 之后 `getAnnounce()` 返回的却是 **tracker1**——成功者只被提升到"自己所在层"的队首，而游标整体归位到**第一层**的第一个 URL。另一个彩蛋：`testSingleElementList` 末尾 `getEventString()` 变成空串，因为 `announceSuccess` 已把该层事件推进到 DOWNLOADING，而 DOWNLOADING 状态的常规 announce 不携带事件参数（[src/AnnounceList.cc:154-172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L154-L172) 的 switch 默认分支返回空串）。make check 的具体过滤输出待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`announce-list` 为 `[[A1,A2],[B1]]`，A1、A2 依次失败后下一次 announce 是谁？之后 B1 也失败呢？
**答案**：A1 失败 → A2（同层备份）；A2 失败 → B1（降层）；B1 失败 → `currentTrackerInitialized_ = false`，`allTiersFailed()` 为真，本轮不再 announce，等下个周期 `resetTier()` 从头再来。这正是 `testSingleAndMulti` 用例（[test/AnnounceListTest.cc:145-165](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/AnnounceListTest.cc#L145-L165)）覆盖的形态。

**练习 2**：`--bt-tracker` 追加的 tracker 为什么拿不到 `AnnounceList::shuffle` 的洗牌待遇？
**答案**：洗牌发生在运行期 `AnnounceList` 上（如 BtSetup 阶段），而 `--bt-tracker` 的追加发生在更早的解析期——`adjustAnnounceUri` 直接往 `TorrentAttribute::announceList` 追加单元素层（[src/bittorrent_helper.cc:1059-1067](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L1059-L1067)）。单元素层内部无序可洗；层间顺序保持"追加在尾"。

**练习 3**：为什么 stopped 事件的发送资格要用 `FindStoppedAllowedTier` 限制，而不是对所有 tier 都发一遍？
**答案**：stopped 表示"我要下线了"，只对曾经成功 announce 过（进入 DOWNLOADING/SEEDING 等状态）的层才有意义；对一个从未成功连接的层发 stopped 只是无意义的流量。运行期代码用 `countStoppedAllowedTier` 判断是否存在可发层、`moveToStoppedAllowedTier` 把游标 wrap 到最近的合法层（[src/AnnounceList.cc:208-241](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AnnounceList.cc#L208-L241)）。

## 5. 综合实践：独立复现 aria2 的 infohash

本讲所有模块可以串成一个任务：**不依赖 aria2 的解析代码，用约 20 行 Python 独立解析一个种子并算出与 aria2 完全相同的 infohash**。能做完，说明你真正吃透了 bencode、infohash 定义与解析流程。

**任务步骤**：

1. 写一个极简 bencode 解码器（示例代码）：

   ```python
   # 示例代码：my_torrent_info.py
   import hashlib, sys

   def decode(buf, pos):
       c = buf[pos:pos+1]
       if c == b'i':                       # 整数
           end = buf.index(b'e', pos)
           return int(buf[pos+1:end]), end + 1
       if c == b'l':                       # 列表
           pos, out = pos + 1, []
           while buf[pos:pos+1] != b'e':
               v, pos = decode(buf, pos); out.append(v)
           return out, pos + 1
       if c == b'd':                       # 字典
           pos, out = pos + 1, {}
           while buf[pos:pos+1] != b'e':
               k, pos = decode(buf, pos)
               v, pos = decode(buf, pos); out[k] = v
           return out, pos + 1
       end = buf.index(b':', pos)          # 字符串
       n = int(buf[pos:end])
       return buf[end+1:end+1+n], end + 1 + n

   def encode(v):                          # 只需覆盖 dict/str/int
       if isinstance(v, dict):
           return b'd' + b''.join(
               encode(k) + encode(v[k]) for k in sorted(v)) + b'e'
       if isinstance(v, bytes):
           return str(len(v)).encode() + b':' + v
       return b'i' + str(v).encode() + b'e'

   buf = open(sys.argv[1], 'rb').read()
   root, _ = decode(buf, 0)
   info = root[b'info']
   print('infohash =', hashlib.sha1(encode(info)).hexdigest().upper())
   print('name =', info[b'name'].decode('utf-8', 'replace'))
   print('piece length =', info[b'piece length'])
   print('num pieces =', len(info[b'pieces']) // 20)
   ```

2. 对仓库自带种子与任一真实种子各跑一次，再与 aria2 对照：

   ```bash
   python3 my_torrent_info.py test/test.torrent
   ./src/aria2c -S test/test.torrent
   ```

3. 比对两边的 infohash、name、piece length、piece 数。

**验收标准**：

- 两边 infohash 逐字符一致（`encode` 中 `sorted(v)` 这一步就是 aria2 侧 `std::map` 有序键的对应物，漏掉它对多数种子不报错、对键未排序的病态种子会算错——想明白为什么）；
- piece 数满足 \( \lceil 总长 / pieceLength \rceil \)，与 4.2 节的一致性检查呼应；
- （可选进阶）在 `src/bittorrent_helper.cc` 的 `processRootDictionary` 中 `torrent->infoHash.assign(...)` 之后（[src/bittorrent_helper.cc:438-440](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/bittorrent_helper.cc#L438-L440) 附近）插入一行日志并在 `extractAnnounce` 的 `push_back` 之后打印各层 URL，重新编译后用 `./src/aria2c -S test/test.torrent` 验证三方一致：

  ```cpp
  // 示例代码：需同时在文件头部补 #include "LogFactory.h"
  A2_LOG_NOTICE(fmt("MYLOG infohash=%s",
                    util::toHex(torrent->infoHash).c_str()));
  ```

  注意默认控制台日志级别是 NOTICE（[src/OptionHandlerFactory.cc:188](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerFactory.cc#L188)），所以示例特意用 `A2_LOG_NOTICE` 而非 `A2_LOG_INFO`，后者需加 `--console-log-level=info` 才可见。加日志的编译与显示效果待本地验证。

## 6. 本讲小结

- .torrent 文件本体是一个 bencode 字典；`bencode2::decode/encode` 是它的双向门面，`Dict` 基于 `std::map` 保证键有序，使"重新编码 info 字典再取 SHA-1"得到的 infohash 与原文截取一致。
- `bittorrent_helper` 的 `load/loadFromMemory` 家族殊途同归到 `processRootDictionary`：校验根/info 字典 → 算 infohash 与 metadata → 读 pieces（每 20 字节一个 piece 哈希）与 piece length → 装配 FileEntry（含目录穿越防护与 `⌈总长/pieceLength⌉ == numPieces` 一致性检查）→ 读 announce/nodes/杂项 → 挂载属性。
- 解析产物按"公共 vs 专属"分流：文件列表、piece 长度、分块哈希进 `DownloadContext` 公共字段（供 u5 存储/校验复用）；infohash、tracker 层级、metadata、私有标志等进 `TorrentAttribute`（BT 属性槽）；magnet 只产出仅含 infohash/name/announceList 的极简属性。
- `AnnounceList` 是解析期快照之上的运行期游标机：tier 内互为备份、失败降层、成功者提至队首并整体归位第一层、全部失败即 `allTiersFailed`；每个 tier 的 `AnnounceTier::event` 状态机（STARTED→DOWNLOADING→…→HALTED）决定本轮 announce 的事件参数与 stopped/completed 的发送资格。

## 7. 下一步学习建议

本讲结束时，种子已经被"读明白"成 `DownloadContext` + `TorrentAttribute`，但还没有任何运行时对象。下一讲 **u6-l2 BtRegistry 与 BtSetup：BT 运行时的装配** 讲这些解析产物如何被装配成 PeerStorage、PieceStorage、BtInteractive 等一整套运行时对象，并以 infohash 为键注册进全局 `BtRegistry`。阅读建议：提前浏览 [src/BtSetup.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/BtSetup.cc) 的 `setup()` 函数，数一数它创建了多少个以 `torrentAttrs`/`downloadContext` 为原料的对象；对 tracker 轮换机制意犹未尽的读者可提前看 u6-l7 的 `DefaultBtAnnounce`（本讲 4.4 已埋下伏笔）。
