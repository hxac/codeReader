# 校验与哈希：MessageDigest 与分块校验

## 1. 本讲目标

下载工具最尴尬的时刻是「下载显示 100% 完成，文件却是坏的」：镜像被入侵、代理注入广告页、磁盘静默出错，都会造成这种结果。本讲讲解 aria2 的数据完整性保障体系。读完本讲，你应该能够：

1. 说清 aria2 有两种校验数据（整文件校验和 vs 分块 piece 哈希）、它们分别从哪里来（`--checksum` 选项、.torrent 的 pieces 字段、Metalink 的 chunk 哈希）。
2. 解释 `MessageDigest` 如何用「一个门面 + 一张静态注册表」把 OpenSSL、libgcrypt、libnettle、Apple CommonCrypto、内置实现等多套哈希后端统一到同一套 `reset/update/digest` API。
3. 跟踪 `IteratableValidator` 迭代校验骨架：为什么校验要拆成一小块一小块做、`CheckIntegrityCommand` 如何驱动它、`CheckIntegrityMan` 如何保证全局同一时刻只校验一个任务。
4. 对比两个校验器实现：整文件校验失败后的「全量重下」与分块校验失败后的「只重下坏 piece」。
5. 理解 `--check-integrity`、`-c`（断点续传）与位图（bitfield）三者如何配合跳过已完成块。

本讲承接 u5-l2（PieceStorage 的 piece 位图模型）与 u3-l2（DownloadContext 元数据模型），是磁盘 I/O 单元的收尾：前面几讲解决「怎么把数据写上盘」，本讲解决「写上去的数据如何证明自己是对的」。

## 2. 前置知识

**哈希函数与摘要（digest）**：哈希函数（如 SHA-256、MD5）把任意长度的数据映射成固定长度的字节串。它的关键特性是：输入哪怕只差 1 个比特，输出也会面目全非。因此发布方公布文件的 SHA-256 值后，下载方重新算一遍并比对，就能以极大概率确认文件没有损坏。注意术语：摘要本身是**原始字节**（SHA-256 是 32 字节），而人们日常粘贴的是它的**十六进制文本**（64 个字符）。aria2 源码里 `digest` 通常是原始字节，界面上显示的是 `util::toHex` 转换后的文本。

**为什么需要「分块」校验**：整文件哈希是「全有或全无」的判定——1 GB 文件里坏了 1 KB，你也只知道「坏了」，不知道坏在哪，只能整个重下。如果把文件切成若干 piece、每个 piece 各配一个哈希（.torrent 的 pieces 字段就是这样），就能精确定位坏块，只重新下载那几兆。代价是要存 N 份哈希、计算粒度变碎。

**为什么校验必须是「可中断的迭代」**：u2-l1 讲过 DownloadEngine 是单线程事件循环。如果对 4 GB 文件一次性算完哈希再返回，事件循环会被卡住几分钟——界面冻结、其他下载停摆、Ctrl-C 无响应。所以 aria2 把校验设计成协程式的「每轮只干一点活」：`validateChunk()` 每次只读 4 KB 更新哈希状态，然后立刻交还控制权，下一轮再来。这正是本讲标题中「Iteratable（可迭代的）」的含义（源码拼写为 Iteratable 而非 Iterable，阅读时注意）。

**位图（bitfield）回顾**：u5-l2 讲过，PieceStorage 用一张完成位图记录哪些 piece 已就绪。本讲会看到：校验器的输出就是「改写这张位图」——校验通过的 piece 在位图上置 1，于是后续下载调度自动跳过它们；整文件校验失败时干脆把位图全部清零，任务退回「什么都没下」的状态。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/Checksum.cc/.h` | 整文件校验和的数据载体：哈希类型 + 摘要字节 |
| `src/ChunkChecksum.cc/.h` | 分块哈希表的数据载体：哈希类型 + 哈希数组 + piece 长度 |
| `src/MessageDigest.cc/.h` | 哈希计算的门面：创建、查询支持、强度比较、update/digest |
| `src/MessageDigestImpl.h` | 后端接口 + 静态注册表（hashType → 工厂函数+摘要长度） |
| `src/LibsslMessageDigestImpl.cc` 等 5 个后端 | 各哈希库的注册与实现（OpenSSL/libgcrypt/libnettle/Apple/内置） |
| `src/message_digest_helper.cc` | 两个一次性算摘要的便捷函数（内存 / BinaryStream） |
| `src/IteratableValidator.h` | 可迭代校验的抽象接口 |
| `src/IteratableChecksumValidator.cc` | 整文件校验器实现 |
| `src/IteratableChunkChecksumValidator.cc` | 分块校验器实现 |
| `src/CheckIntegrityEntry.cc/.h` | 校验任务条目基类：持有 validator，衔接文件分配 |
| `src/ChecksumCheckIntegrityEntry.cc`、`src/PieceHashCheckIntegrityEntry.cc`、`src/StreamCheckIntegrityEntry.cc` | 三个入口子类，决定用哪个校验器、失败后怎么办 |
| `src/CheckIntegrityCommand.cc`、`src/CheckIntegrityMan.h` | 驱动校验的命令与「停车场」队列 |
| `src/RequestGroup.cc`（两段） | 校验任务的创建时机与三条件闸门 |
| `src/DownloadCommand.cc`（一段） | HTTP/FTP 下载完成后追加整文件校验的入口 |
| `src/OptionHandlerImpl.cc`（一段）、`src/download_helper.cc`（一段） | `--checksum` 选项的解析与落到 DownloadContext |
| `src/DownloadContext.cc`（一段） | 「可校验 / 需校验」的判定函数 |

## 4. 核心概念与源码讲解

### 4.1 校验数据的载体：Checksum、ChunkChecksum 与 --checksum 选项

#### 4.1.1 概念说明

校验的第一步是「手里先有标准答案」。aria2 用两个纯数据类承载答案：

- `Checksum`：整文件校验和，只有两个字段——哈希类型（如 `"sha-256"`）和摘要（原始字节串）。对应命令行 `--checksum=sha-256=<hex>`、Metalink 的 `<hash>` 元素、HTTP 头 `X-Checksum-*` 等。
- `ChunkChecksum`：分块哈希表，字段为哈希类型、每个 piece 一条的哈希数组、piece 长度。对应 .torrent 的 `pieces` 字段和 Metalink 的 `<hash type="sha-1" piece="...">` 列表。

值得注意的设计：这两个类只是「搬运工」。任务真正运行时，数据被拆进 `DownloadContext` 的四个字段（`hashType_`/`digest_` 与 `pieceHashType_`/`pieceHashes_`，见 u3-l2）。`ChunkChecksum` 对象本身主要活在 Metalink 解析器（`MetalinkParserController`）和 `Metalink2RequestGroup` 里，作为「解析产物 → RequestGroup」的中转格式。校验器读的都是 DownloadContext，不直接持有这两个类。

#### 4.1.2 核心流程

以 `--checksum=sha-256=abc...` 下载一个 HTTP 文件为例：

```
命令行/配置文件
   │  OptionParser 把文本 "sha-256=abc..." 存进 Option（仍是字符串）
   ▼
ChecksumOptionHandler::parseArg          ── 校验格式：类型支持？hex？长度=2×摘要长度？
   ▼
download_helper::createRequestGroupForUri ── 拆分 "类型=摘要"，setDigest 写入 DownloadContext
   ▼
DownloadContext { hashType_="sha-256", digest_=<32字节> }
   ▼
（下载完成后的校验入口见 4.3 节）
```

判定「这份任务到底能不能校验、需不需要校验」的是 DownloadContext 上的三个函数：

- `isChecksumVerificationAvailable()`：有整文件摘要即可（`!digest_.empty() && !hashType_.empty()`）。
- `isPieceHashVerificationAvailable()`：有 piece 哈希且**条数必须恰好等于 piece 总数**（哈希表残缺时拒绝分块校验）。
- `isChecksumVerificationNeeded()`：可校验且还没校验过、且**没有** piece 哈希（有 piece 哈希时整文件摘要退居二线）。

#### 4.1.3 源码精读

`Checksum` 的全部逻辑就是一对 getter/setter 加一个「是否为空」，默认哈希类型是 sha-1：

- [src/Checksum.cc:40-49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Checksum.cc#L40-L49) —— 构造函数存入 `hashType` 与 `digest` 两个字符串；`isEmpty()` 用「摘要为空」表示「没有校验和」。
- [src/Checksum.cc:69-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Checksum.cc#L69-L73) —— `HashTypeStronger` 比较器，委托给 `MessageDigest::isStronger`（Metalink 给同一文件提供多种哈希时，用来挑最强的那个）。

`ChunkChecksum` 增加了「按索引取哈希」和「估算长度」：

- [src/ChunkChecksum.cc:42-49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkChecksum.cc#L42-L49) —— 构造时一次性收走哈希类型、哈希数组与 piece 长度。
- [src/ChunkChecksum.cc:51-56](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkChecksum.cc#L51-L56) —— `validateChunk`：给定第 index 块的实际摘要，与表中该项比对；注意 `!digest.empty()` 这个前置条件——表项为空视为「不匹配」而非「跳过」。
- [src/ChunkChecksum.cc:58-73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChunkChecksum.cc#L58-L73) —— `getEstimatedDataLength()` 用「piece 长度 × 哈希条数」估算总长（Metalink 元数据缺 size 时兜底）；`getPieceHash(index)` 越界时返回空串 `A2STR::NIL` 而不是抛异常。

`--checksum` 选项的入口校验：

- [src/OptionHandlerImpl.cc:380-398](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/OptionHandlerImpl.cc#L380-L398) —— `ChecksumOptionHandler::parseArg` 把参数按第一个 `=` 切成类型与十六进制摘要，两段都转小写后交给 `MessageDigest::isValidHash` 检查；不合格直接抛 `Unrecognized checksum`。这就是你写错哈希长度时立刻看到报错的位置。
- [src/download_helper.cc:145-153](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L145-L153) —— 任务创建时把 `PREF_CHECKSUM` 的文本再拆一次，`util::fromHex` 把十六进制**解码成原始字节**后 `dctx->setDigest(...)` 写入 DownloadContext。这印证了前文：aria2 内部存原始字节，界面显示时才 `toHex`。

三个判定函数（校验体系的「红绿灯」）：

- [src/DownloadContext.cc:260-275](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.cc#L260-L275) —— `isChecksumVerificationNeeded()` 要求「无 piece 哈希 + 有整文件摘要 + 尚未校验过」；`isChecksumVerificationAvailable()` 只看有没有摘要；`isPieceHashVerificationAvailable()` 额外要求哈希条数与 piece 数严格相等。

piece 哈希进入 DownloadContext 的通道（.torrent / Metalink 都走这里）：

- [src/DownloadContext.h:110-115](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadContext.h#L110-L115) —— `setPieceHashes` 模板函数把迭代器区间灌进 `pieceHashes_`，同时记下 `pieceHashType_`。

#### 4.1.4 代码实践

**实践目标**：亲手触发一次「校验和错误」，观察 aria2 拒绝坏文件的完整行为链。

**操作步骤**：

1. 准备一个测试文件并启动本地 HTTP 服务（示例命令，在单独目录执行）：

   ```bash
   mkdir /tmp/aria2-cs && cd /tmp/aria2-cs
   head -c 2000000 /dev/urandom > test.bin        # 2 MB 随机文件
   sha256sum test.bin                               # 记下正确的 64 位十六进制
   python3 -m http.server 8000                      # 起一个本地 HTTP 服务
   ```

2. 用**错误的**校验和下载（把正确值的前 4 个字符换成 `0000`）：

   ```bash
   ./src/aria2c --checksum=sha-256=0000<剩余60位十六进制> \
       --allow-overwrite=true -d /tmp/aria2-cs/out \
       http://127.0.0.1:8000/test.bin
   ```

3. 再故意写一个格式非法的校验和（长度不足 64 位）：

   ```bash
   ./src/aria2c --checksum=sha-256=abcd http://127.0.0.1:8000/test.bin
   ```

4. 用正确的校验和重复步骤 2，作为对照组。

**需要观察的现象**：

- 步骤 2：下载本身正常完成，随后控制台/日志出现 `error code: 32`（`CHECKSUM_ERROR`）；开启 `--log=... --log-level=info` 能看到 `Checksum validation failed. expected=..., actual=...` 一行（它的出处在本讲 4.4 节）。
- 步骤 3：任务还没开始下载就被拒绝，报 `Unrecognized checksum`——校验发生在选项解析阶段。
- 步骤 4：日志出现校验成功（Verification finished / download completed），退出码 0。

**预期结果**：aria2 对坏校验和的处置是「报告 CHECKSUM_ERROR 并结束任务」，默认**不会**自动重新下载（原因见 4.4 节 `redownload_` 标志）。退出码 32 与 [src/error_code.h:78](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/error_code.h#L78) 中定义的 `CHECKSUM_ERROR = 32` 对应。完整命令输出请以本地运行为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：`--checksum=sha-256=<hex>` 中 hex 为什么必须是 64 个字符？哪行代码执行这个检查？

答案：SHA-256 摘要是 32 字节，十六进制文本每 2 个字符表示 1 字节，故为 64 字符。检查在 `MessageDigest::isValidHash` 中（见 [src/MessageDigest.cc:135-140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigest.cc#L135-L140)），条件是 `getDigestLength(hashType) * 2 == hexDigest.size()`，并要求全为十六进制字符且哈希类型受支持；该函数被 `ChecksumOptionHandler::parseArg` 调用。

**练习 2**：一个 .torrent 文件声明 piece 长度 256 KB、文件总长 1 MB，但 pieces 字段只有 3 条哈希（应为 4 条）。`isPieceHashVerificationAvailable()` 返回什么？为什么要这么设计？

答案：返回 false，因为函数要求 `pieceHashes_.size() == getNumPieces()`（1 MB ÷ 256 KB = 4 ≠ 3）。设计动机：哈希表残缺时无法对每个 piece 给出判定，与其「校验到一半发现没答案」不如一开始就拒绝分块校验，避免产生误导性的位图。

**练习 3**：为什么 `Checksum::getDigest()` 存的是原始字节而命令行上用户输入的是十六进制？转换发生在哪？

答案：哈希库（OpenSSL 等）输出和比对的均是原始字节，内部统一存字节可避免反复编解码。命令行输入的十六进制在 [src/download_helper.cc:145-153](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/download_helper.cc#L145-L153) 被 `util::fromHex` 解码后写入；日志展示时再用 `util::toHex` 编码回去。

### 4.2 MessageDigest：统一多哈希后端的门面

#### 4.2.1 概念说明

不同平台能拿到的哈希库不同：Linux 上常见 OpenSSL 或 GnuTLS（配 libgcrypt/libnettle），macOS 可用 CommonCrypto，Windows 有 BCrypt，实在不行 aria2 还自带一份内置实现。这些库的 API 毫无共性——OpenSSL 用 `EVP_DigestInit_ex/EVP_DigestUpdate/EVP_DigestFinal_ex`，gcrypt 用 `gcry_md_open/write/read`。

`MessageDigest` 用两层结构屏蔽差异：

1. **门面层 `MessageDigest`**：业务代码只看到 `create("sha-256")`、`update(data, len)`、`digest()`，以及一批静态查询（支持哪些类型、摘要多长、谁更强）。内部用 pImpl 惯用法持有真正干活的 `MessageDigestImpl`。
2. **注册表层 `MessageDigestImpl`**：定义纯虚接口（`getDigestLength/reset/update/digest`）外加一张**静态 map**：键是哈希名字符串，值是「无参工厂函数 + 摘要长度」二元组。每个后端源文件各自实例化这张表，链接进二进制的后端只有一个，于是查表天然只会命中本后端支持的算法。

这与 u1 学过的「configure 探测 → 编译宏 → 条件编译」一脉相承：**后端选择发生在编译期，运行期只是一次 map 查找**。

#### 4.2.2 核心流程

```
业务代码: MessageDigest::create("sha-256")
   │  门面直接转发
   ▼
MessageDigestImpl::create(hashType)
   │  在静态表 hashes 中查 "sha-256"
   │    命中 → 调工厂函数 new 一个后端对象
   │    未命中 → 返回 nullptr（上层随即抛"不支持的哈希"）
   ▼
门面包装成 MessageDigest 返回
   │
   ▼
update(...) × N 次  →  digest() 取出最终摘要（对象可 reset 后复用）
```

注册侧（编译期）的形状：每个后端 .cc 里都有一行 `MessageDigestImpl::hashes_t MessageDigestImpl::hashes = { {"sha-1", make_hi<...>()}, ... };`，静态成员在程序启动前完成初始化，运行期只读。

#### 4.2.3 源码精读

门面公共 API（业务代码全部依赖这一层）：

- [src/MessageDigest.h:59-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigest.h#L59-L90) —— 工厂 `sha1()`/`create()`、查询 `supports`/`getSupportedHashTypes`/`getDigestLength`/`isStronger`/`isValidHash`/`getCanonicalHashType`，逐条有注释说明语义。

注册表与后端接口：

- [src/MessageDigestImpl.h:52-60](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigestImpl.h#L52-L60) —— 定义 `factory_t`（返回 impl 的无参函数）、`hash_info_t`（工厂+长度二元组）、`hashes_t`（名字→信息的 map）；`make_hi<T>()` 用 lambda 把「new T」打包成工厂并附上 `T::length()`。
- [src/MessageDigestImpl.h:72-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigestImpl.h#L72-L95) —— `create`（查表调工厂，查不到返回 nullptr）、`supports`、`getDigestLength(hashType)` 三个静态内联函数，全部是纯查表操作。
- [src/MessageDigestImpl.h:97-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigestImpl.h#L97-L101) —— 四个纯虚函数构成后端契约。

以 OpenSSL 后端为例看注册与实现：

- [src/LibsslMessageDigestImpl.cc:63-97](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslMessageDigestImpl.cc#L63-L97) —— 类模板 `MessageDigestBase<init_fn>` 把任意 `EVP_md*` 函数封装成 impl：构造建 `EVP_MD_CTX`，`reset` 调 `EVP_DigestInit_ex`，`update` 调 `EVP_DigestUpdate`（按 `uint32_t` 上限分片喂入），`digest` 调 `EVP_DigestFinal_ex`。模板参数即算法，一个模板产出全部算法类。
- [src/LibsslMessageDigestImpl.cc:107-122](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibsslMessageDigestImpl.cc#L107-L122) —— 静态表实例化：`sha-1`、`md5` 无条件注册，sha-224/256/384/512 各自由 `HAVE_EVP_SHA224` 等编译宏守护（老版本 OpenSSL 可能没有这些算法）；`ADLER32_MESSAGE_DIGEST` 依赖 zlib。**这张表的存在本身就声明了本后端的能力边界。**

其余四个后端的注册表位置（形状与 OpenSSL 版完全一致）：

- [src/LibgcryptMessageDigestImpl.cc:109](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibgcryptMessageDigestImpl.cc#L109)、[src/LibnettleMessageDigestImpl.cc:93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/LibnettleMessageDigestImpl.cc#L93)、[src/AppleMessageDigestImpl.cc:99](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AppleMessageDigestImpl.cc#L99)、[src/InternalMessageDigestImpl.cc:96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/InternalMessageDigestImpl.cc#L96) —— 五个后端各自实例化同一个静态成员 `MessageDigestImpl::hashes`，链接器只会保留实际编入的那一份。

门面层的静态查询实现：

- [src/MessageDigest.cc:57-64](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigest.cc#L57-L64) —— `hashTypes` 强度表：sha-1=1、sha-224=2、sha-256=3、sha-384=4、sha-512=5，md5 与 adler32 均为 0。强度是人为编排的序数，不是安全强度数值。
- [src/MessageDigest.cc:118-140](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigest.cc#L118-L140) —— `isStronger` 按强度表比较两个类型（供 Metalink 在多个可用哈希中挑最强）；`isValidHash` 是 4.1 练习 1 提到的入口校验。
- [src/MessageDigest.cc:142-156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigest.cc#L142-L156) —— `getCanonicalHashType`：把 Metalink3 时代的 `sha1`/`sha256` 别名归一成 `sha-1`/`sha-256`（向后兼容的别名表）。
- [src/MessageDigest.cc:158-179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigest.cc#L158-L179) —— 实例方法 `getDigestLength/reset/update/digest`，全部一行转发给 pImpl；`digest()` 无参版本自动分配缓冲并返回字节串。

编译期如何决定链接哪个后端（呼应 u1-l2）：

- [src/Makefile.am:356-358](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L356-L358) —— 无外部哈希库时编入 `InternalMessageDigestImpl.cc` + `crypto_hash.cc`（自带实现，也是 Windows 配置的缺省）。
- [src/Makefile.am:371](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L371)、[src/Makefile.am:377-379](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L377-L379)、[src/Makefile.am:396-399](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L396-L399)、[src/Makefile.am:329-330](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/Makefile.am#L329-L330) —— libgcrypt、libnettle、OpenSSL、Apple 四套条件块，`USE_*_MD` 宏择一编入。

`message_digest_helper` 提供的两个「一把梭」便捷函数（被 11 处调用：torrent infohash、DHT token、MSE 握手密钥、RPC secret、WebSocket key 等）：

- [src/message_digest_helper.cc:52-75](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message_digest_helper.cc#L52-L75) —— `digest(ctx, BinaryStream, offset, length)`：从任意 BinaryStream（磁盘文件、内存写入器都行）的指定区间分 4 KB 块读出并喂给哈希，读不满即抛 `data is too short`。
- [src/message_digest_helper.cc:77-89](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/message_digest_helper.cc#L77-L89) —— `digest(md, mdLength, ctx, data, length)`：对一块内存数据一次算完，先检查输出缓冲够大，防止溢出。

#### 4.2.4 代码实践

**实践目标**：确认你手上这个 aria2 二进制实际编入了哪个哈希后端、支持哪些算法。

**操作步骤**：

1. 运行 `./src/aria2c --version`，查看 `Features:` 一行是否出现 `GnuTLS`/`OpenSSL`/`Apple TLS`/`Windows TLS` 等字样（TLS 库与哈希后端通常配套，见 u1-l2 的特性推导）。
2. 源码阅读：用 `grep -n "MessageDigestImpl::hashes = " src/*.cc` 列出全部五处静态表定义，再对照 `src/Makefile.am` 的五个条件块，推断「你编译时的 configure 选项 → 哪张表被链接」。
3. 验证能力查询链：找到 [src/MessageDigest.cc:89-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MessageDigest.cc#L89-L98) 的 `getSupportedHashTypes`——它遍历固定名单逐个调 `supports`，只返回真被注册的类型。`aria2c --version` 的哈希能力与 `--checksum` 可接受的类型都由它背后这张表决定。
4. 试着给出一个本后端不支持的类型：`./src/aria2c --checksum=sha-1024=<64位hex> URL`。

**需要观察的现象**：步骤 4 立即报 `Unrecognized checksum`；因为 `sha-1024` 查表落空，`supports` 返回 false。

**预期结果**：`Features` 行与你 configure 输出一致；五处 `hashes` 定义中只有一处被实际链接（其余源文件未参与编译）。具体输出待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MessageDigestImpl::create` 查表失败返回 `nullptr` 而不是抛异常？异常由谁负责抛？

答案：impl 层是底层库性质的代码，保持「可查询、可失败」的纯函数风格；上层 `MessageDigest::create` 的调用方（如 `IteratableChecksumValidator::init`）拿到对象直接使用，真正的用户可见报错发生在更外层——例如 `ChecksumOptionHandler::parseArg` 在任务创建前就用 `supports` 拦截了不支持的类型，避免下载到一半才失败。

**练习 2**：OpenSSL 后端里 sha-256 的注册为什么包在 `#ifdef HAVE_EVP_SHA224` 里？如果编译环境没有这个宏，运行期会怎样？

答案：老版本 OpenSSL 的 libcrypto 缺少 SHA-2 家族的 EVP 接口，宏由 configure 探测 `EVP_sha256` 等符号是否存在来定义。没有宏时 `hashes` 表里就没有 `sha-256` 项，运行期 `MessageDigest::supports("sha-256")` 返回 false，`--checksum=sha-256=...` 在选项解析阶段即被拒绝。

**练习 3**：`update` 为什么要按 `uint32_t` 上限分片循环喂入（OpenSSL 后端）？

答案：部分哈希库的 update 长度参数是 32 位，一次喂超过 4 GB 的缓冲会溢出截断，算出错误摘要。分片循环保证任何长度的输入都被正确消化——这是多后端适配中典型的「接口最窄化」处理。

### 4.3 IteratableValidator 骨架：CheckIntegrityEntry 家族与调度循环

#### 4.3.1 概念说明

有了数据和哈希器，还差一个「执行者」。执行者要满足三个约束：不阻塞事件循环（每轮只干一小块）、有进度可上报（给控制台/RPC 显示百分比）、能被串行化（校验是大流量读盘，几个任务同时校验会把磁盘打满）。

aria2 的答案是一套四件套：

- **`IteratableValidator`**（抽象接口）：`init()` → 反复 `validateChunk()` → 直到 `finished()`，随时可用 `getCurrentOffset()/getTotalLength()` 报进度。这就是「可中断迭代校验」的契约。
- **`CheckIntegrityEntry`**（任务条目）：继承自 RequestGroupEntry（u3-l1 的「停车场」条目），持有一个 validator，并提供 `onDownloadFinished/onDownloadIncomplete` 两个「校验结果回调」。
- **`CheckIntegrityMan`**：就是 `SequentialPicker<CheckIntegrityEntry>` 的类型别名——与文件预分配的 FileAllocationMan（u5-l4）同构的单槽停车场，全局同一时刻只有一个任务在校验。
- **`CheckIntegrityCommand`**（RealtimeCommand）：瞬时 worker，每轮执行一次 `validateChunk()`，没干完就把自己重新排队。

三个 Entry 子类决定「用哪个校验器 + 失败后怎么办」：

| 子类 | isValidationReady 条件 | initValidator 造的校验器 | 校验失败（仍有缺块）时的处置 |
| --- | --- | --- | --- |
| `ChecksumCheckIntegrityEntry` | 有整文件摘要 | `IteratableChecksumValidator` | `redownload_` 为 true 则重新走文件分配全量重下；否则记错误码 CHECKSUM_ERROR 结束 |
| `PieceHashCheckIntegrityEntry` | 有完整 piece 哈希表 | `IteratableChunkChecksumValidator` | 抽象基类（由子类定义） |
| `StreamCheckIntegrityEntry`（继承上者） | 同上 | 同上 | `onDownloadIncomplete()` 更新缺块，进入文件分配/下载阶段（`--hash-check-only` 时到此为止） |
| `BtCheckIntegrityEntry`（继承 PieceHash，u6-l11 详讲） | 同上 | 同上 | 衔接 BT 运行时装配 |

#### 4.3.2 核心流程

一次完整的校验调度（以断点续传 + `--check-integrity=true` 为例）：

```
RequestGroup::createInitialCommand
   └─ createCheckIntegrityEntry()            ── 按「--check-integrity？piece哈希？.aria2控制文件？文件已存在？」四问选 Entry 子类
        │
        ▼
RequestGroup::processCheckIntegrityEntry()
   ├─ 实际文件 > 总长 ？ → cutTrailingGarbage() 截掉尾部垃圾
   ├─ 三条件闸门：(--check-integrity 为 true 或 isChecksumVerificationNeeded)
   │            且 entry->isValidationReady()
   │      ├─ 满足 → initValidator() + disableSaveControlFile()
   │      │         push 进 CheckIntegrityMan（停车场）
   │      └─ 不满足 → 直接 onDownloadIncomplete()，跳过校验进入下载
        ▼
CheckIntegrityDispatcherCommand（常驻，u5-l4 同款 dispatcher/worker 模式）
   └─ 从停车场取出队首 entry，铸造 CheckIntegrityCommand（worker）
        ▼  每轮事件循环
CheckIntegrityCommand::executeInternal
   ├─ entry_->validateChunk()     ── 4 KB 或一个 piece
   ├─ 未 finished → addCommandSelf 重新排队（进度经 ConsoleStatCalc 显示 [Checksum:...%]）
   └─ finished → enableSaveControlFile()
        ├─ 任务 downloadFinished() → 日志「校验成功」→ onDownloadFinished
        └─ 否则 → 日志「校验失败」→ onDownloadIncomplete（重下坏块或报错）
```

注意 `disableSaveControlFile` 的细节：校验期间禁止写 .aria2 控制文件，防止用户此刻 Ctrl-C 后留下「已完成长度为 0」的迷惑控制文件；校验结束后由 CheckIntegrityCommand 重新启用。

#### 4.3.3 源码精读

迭代校验契约（注释即文档）：

- [src/IteratableValidator.h:52-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableValidator.h#L52-L65) —— 五个纯虚函数；注释明确要求「先 init 再循环 validateChunk 直到 finished」，进度用 offset/totalLength 表达。两个校验器实现（4.4/4.5）都精确满足此契约。

停车场与 worker：

- [src/CheckIntegrityMan.h:45](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityMan.h#L45) —— `typedef SequentialPicker<CheckIntegrityEntry> CheckIntegrityMan;` 一行别名。单槽 picker 语义见 u5-l4：同一时刻只 pick 一个条目。
- [src/CheckIntegrityCommand.cc:64-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityCommand.cc#L64-L98) —— worker 主循环：每轮只调**一次** `entry_->validateChunk()`；没结束就把自己重新加回命令队列（`addCommand` + 返回 false，符合 u2-l3 的命令自排队约定）；结束后重新启用控制文件保存，按 `downloadFinished()` 二选一回调，并 `setNoWait(true)` 让引擎立刻结算。haltRequested 时直接退出。
- [src/CheckIntegrityCommand.cc:59-62](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityCommand.cc#L59-L62) —— 析构时 `dropPickedEntry()` 归还停车场槽位，这是 dispatcher/worker 模式的「完成后放行下一个」钩子。

Entry 基类：

- [src/CheckIntegrityEntry.cc:55-77](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityEntry.cc#L55-L77) —— `validateChunk/finished/getTotalLength/getCurrentLength` 全部一行转发给 validator；validator 未设置时长度按 0 报告（所以刚 push 进停车场、还没 initValidator 的条目显示 0%）。
- [src/CheckIntegrityEntry.cc:79-94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityEntry.cc#L79-L94) —— `cutTrailingGarbage`（磁盘文件比元数据总长还长时截尾）与 `proceedFileAllocation`（校验完接着走文件分配，衔接 u5-l4 的第二停车场）。

入口选择（谁决定用哪个子类）：

- [src/RequestGroup.cc:221-269](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L221-L269) —— `createCheckIntegrityEntry` 的四分支：① `--check-integrity` 且有 piece 哈希 → StreamCheckIntegrityEntry（分块校验，不在乎文件是否下载过）；② 控制文件存在（或 `-c` 且文件存在）且文件其实已下完但校验没做 → ChecksumCheckIntegrityEntry 并 `setRedownload(true)`（校验失败全量重下）；已下完且无需校验 → 直接宣告完成返回 nullptr；③ 文件长度恰好等于总长且有整文件摘要 → 同样 redownload=true；④ 兜底 StreamCheckIntegrityEntry。**这段代码就是「--check-integrity 与断点续存配合」的核心**。
- [src/RequestGroup.cc:529-553](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L529-L553) —— `processCheckIntegrityEntry` 的闸门：先 `cutTrailingGarbage`，再判断「`--check-integrity` 为真 **或** `isChecksumVerificationNeeded()`（未校验的整文件摘要）」且 Entry 自认 ready，才 initValidator 并 push 停车场；否则直接 `onDownloadIncomplete` 跳过校验。中间大段注释解释了校验期间禁写控制文件的原因。
- [src/RequestGroup.cc:166-172](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L166-L172) —— `isCheckIntegrityReady`：RPC `getFiles` 等处用来报告「本任务已准备好校验」。

下载完成后追加整文件校验的第三入口（普通 HTTP/FTP 下载走这里，而非 createCheckIntegrityEntry）：

- [src/DownloadCommand.cc:317-338](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L317-L338) —— `prepareForNextSegment` 检测到下载完成且**没有 piece 哈希**时，创建 ChecksumCheckIntegrityEntry，`isValidationReady` 通过即 initValidator + `cutTrailingGarbage` + push 停车场。注意这里**没有** setRedownload(true)，所以普通下载完成后校验失败就是报 CHECKSUM_ERROR，不重下——这解释了 4.1.4 实践中观察到的行为。
- [src/HttpResponseCommand.cc:435-441](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/HttpResponseCommand.cc#L435-L441) —— 零长度文件的特例入口（文件已存在且长度匹配时补一次校验），FTP 侧 FtpNegotiationCommand 有对称代码。

三个子类的差异点：

- [src/ChecksumCheckIntegrityEntry.cc:56-70](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChecksumCheckIntegrityEntry.cc#L56-L70) —— ready 条件 = `isChecksumVerificationAvailable`；initValidator 造 `IteratableChecksumValidator`。
- [src/ChecksumCheckIntegrityEntry.cc:77-90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ChecksumCheckIntegrityEntry.cc#L77-L90) —— 失败处置双分支：`redownload_` 为真则 `proceedFileAllocation` 推入 StreamFileAllocationEntry 从头重下；为假则 `setLastErrorCode(CHECKSUM_ERROR)` 结束任务。**「校验失败是否重下」完全由这个布尔标志决定，而它只在断点续传/文件已存在场景被置 true。**
- [src/PieceHashCheckIntegrityEntry.cc:52-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/PieceHashCheckIntegrityEntry.cc#L52-L66) —— ready 条件 = `isPieceHashVerificationAvailable`；initValidator 造 `IteratableChunkChecksumValidator`。
- [src/StreamCheckIntegrityEntry.cc:53-65](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/StreamCheckIntegrityEntry.cc#L53-L65) —— 失败（有缺块）时先 `ps->onDownloadIncomplete()` 让 PieceStorage 感知缺块，`--hash-check-only` 则到此为止（纯校验模式），否则进入文件分配 → 下载，只补缺块。

进度显示（观察校验进行中的窗口）：

- [src/ConsoleStatCalc.cc:369-385](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/ConsoleStatCalc.cc#L369-L385) —— 进度行追加 `[Checksum:#gid 已校验/总长(百分比)]`，队列里还有任务时追加 `(+N)`。

#### 4.3.4 代码实践

**实践目标**：用日志观察一次真实的迭代校验过程，验证「每轮 4 KB」与「校验期间进度行」。

**操作步骤**：

1. 重复 4.1.4 的下载，但加上详细日志与强制校验：

   ```bash
   ./src/aria2c --checksum=sha-256=<正确值> --check-integrity=true \
       --allow-overwrite=true --log=/tmp/cs.log --log-level=info \
       -d /tmp/aria2-cs/out http://127.0.0.1:8000/test.bin
   ```

2. 源码阅读：对照 [src/CheckIntegrityCommand.cc:64-98](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/CheckIntegrityCommand.cc#L64-L98)，在 `validateChunk()` 调用前后各加一行 `A2_LOG_INFO`（例如打印 `getCurrentLength()`），重新编译。
3. 重跑并查看日志时间序列；同时盯着终端进度行里的 `[Checksum:...]` 段。

**需要观察的现象**：日志中每轮校验前进度单调递增（2 MB 文件、4 KB 块约 500 轮，通常瞬间完成，可用更大的文件放大效果）；进度行从 `[Checksum:#xxxx 0B/2.0MiB(0%)]` 走到 `100%`；每轮之间夹杂其他下载命令的日志（证明校验没有独占事件循环）。

**预期结果**：校验与下载、其他任务的处理交错进行；校验完成后日志出现校验成功通告。轮次具体数目取决于文件大小（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`CheckIntegrityMan` 为什么用单槽 SequentialPicker 而不是让多个任务并行校验？

答案：校验是连续大流量读盘 + CPU 哈希，多任务并行会互相争抢磁盘带宽与 CPU 缓存，总吞吐反而下降，且进度展示混乱。单槽串行化（与文件预分配 FileAllocationMan 同构）保证全局只有一个校验 worker 在跑，其余任务在停车场排队——`ConsoleStatCalc` 的 `(+N)` 显示的就是排队数。

**练习 2**：`processCheckIntegrityEntry` 的闸门条件是「`--check-integrity` 为真**或** `isChecksumVerificationNeeded()`」而不是仅「`--check-integrity` 为真」。为什么即使不加 `--check-integrity` 有时也会校验？

答案：`isChecksumVerificationNeeded()` 为真表示任务带着整文件摘要却还没校验过（例如 `-c` 续传一个已完成的文件）。此时若不校验，aria2 就会把「没验证过的文件」当成功上报，违背「提供校验和就必须验证」的承诺；所以这个条件被设计成兜底强制项。相关分支见 [src/RequestGroup.cc:537-539](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L537-L539)。

**练习 3**：为什么 `CheckIntegrityCommand::executeInternal` 在结束时调用 `setNoWait(true)` 和 `setRefreshInterval(0)`？

答案：校验刚结束时引擎可能正处在长轮询等待中；这两行强制下一轮循环立即执行、刷新间隔归零，使「校验完成 → 派生后续命令/结算任务结果」之间没有一秒的延迟，与 u2-l1 讲的引擎刷新机制配合。

### 4.4 整文件校验器：IteratableChecksumValidator

#### 4.4.1 概念说明

`IteratableChecksumValidator` 是两个校验器中较简单的一个，对应「只有一个总摘要、不知道坏在哪」的场景。它的策略是：

- 把文件从头到尾按 4 KB 一块读出，持续喂给一个 `MessageDigest`；
- 读到总长度处（`finished()`）取最终摘要与期望值比对；
- **成功** → `markAllPiecesDone()` 把完成位图全部置 1 + `setChecksumVerified(true)`，任务宣告完成；
- **失败** → 构造一张**全零**的 BitfieldMan 位图写回 PieceStorage，任务瞬间退回「零字节已下载」状态；之后走不走重下，由 4.3 节 Entry 的 `redownload_` 决定。

注意它是「读磁盘上的真实文件」而不是「读下载缓冲」，所以断点续传时它能发现「上次下了一半的文件其实已经损坏」。读的方法是 `readDataDropCache`——读完顺手把这部分页缓存丢掉（`dropCache`），避免校验几个 GB 的文件把操作系统页缓存全部冲掉、拖累其他任务的读写。

#### 4.4.2 核心流程

```
init():  currentOffset_ = 0; ctx_ = MessageDigest::create(整文件哈希类型)
loop:
  validateChunk():
    buf = readDataDropCache(4KB, currentOffset_)   # 从 DiskAdaptor 按逻辑偏移读
    ctx_->update(buf)
    currentOffset_ += 实读长度
    if currentOffset_ >= totalLength:              # finished()
      actual = ctx_->digest()
      actual == 期望 ? markAllPiecesDone() + setChecksumVerified(true)
                     : 位图清零（全零 bitfield 写回 PieceStorage）
```

零长度文件是一个被注释特别关照的边界：`validateChunk` 故意不在入口检查 `finished()`，让第一次调用（读到 0 字节、立即 finished）也能完成摘要比对，保证空文件可校验。

#### 4.4.3 源码精读

- [src/IteratableChecksumValidator.cc:62-86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChecksumValidator.cc#L62-L86) —— `validateChunk` 全部逻辑。注释解释了为何不用 `!finished()` 守卫（零长度文件）；读盘走 `readDataDropCache`；成功路径 `markAllPiecesDone + setChecksumVerified`；失败路径先 `A2_LOG_INFO` 打出期望/实际摘要（4.1.4 实践看到的那行日志就来自这里），再new 一个以「piece 长度×总长」为参数的空 `BitfieldMan`（默认全 0）写回 PieceStorage——**这一步就是「校验失败 = 进度清零」的实现**。
- [src/IteratableChecksumValidator.cc:88-96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChecksumValidator.cc#L88-L96) —— `finished()`：`currentOffset_ >= totalLength` 即完成。
- [src/IteratableChecksumValidator.cc:103-107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChecksumValidator.cc#L103-L107) —— `init()`：偏移归零、按 DownloadContext 的整文件哈希类型创建 MessageDigest。哈希对象的创建被推迟到 init，保证查表发生在正确的时点。

`readDataDropCache` 的语义（为何叫 DropCache）：

- [src/AbstractSingleDiskAdaptor.cc:85-95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/AbstractSingleDiskAdaptor.cc#L85-L95) —— 读完后对刚读区间调 `diskWriter_->dropCache(len, offset)`（即 posix_fadvise(DONTNEED) 一类的提示），把页缓存还给系统。多文件任务在 MultiDiskAdaptor 有对应实现（[src/MultiDiskAdaptor.cc:366](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/MultiDiskAdaptor.cc#L366)）。

#### 4.4.4 代码实践

**实践目标**：验证「整文件校验失败 → 进度清零 → 是否重下取决于入口」，即 `redownload_` 标志的两个取值分别对应什么行为。

**操作步骤**：

1. **场景 A（下载完成后校验，不重下）**：完全重复 4.1.4 的错误校验和下载，记录退出码与文件是否保留。
2. **场景 B（断点续传时校验，失败全量重下）**：先用正确校验和完整下载一次（保留输出文件），然后删掉 `.aria2` 控制文件（若存在），再带着**错误**校验和与续传开关重新运行：

   ```bash
   ./src/aria2c --checksum=sha-256=0000<剩余60位> --check-integrity=true -c \
       --allow-overwrite=true -d /tmp/aria2-cs/out http://127.0.0.1:8000/test.bin
   ```

3. 源码对照：场景 A 走 [src/DownloadCommand.cc:330-338](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L330-L338)（redownload_ 默认 false）；场景 B 走 [src/RequestGroup.cc:258-265](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L258-L265)（文件长度匹配分支，`setRedownload(true)`）。

**需要观察的现象**：

- 场景 A：下载完成 → 校验 → 报 `error code: 32`，任务失败结束，aria2 **没有**再次发起下载请求（可在 HTTP 服务端日志确认只有一轮 GET）。
- 场景 B：发现文件已存在且长度匹配 → 校验 → 失败 → **重新**发起 GET 下载（服务端日志出现第二次请求），再次校验再次失败，最终仍以 32 退出。日志中可见位图清零后下载进度从头开始。

**预期结果**：两个场景的差异完全由 `redownload_` 解释——场景 B 中校验失败触发 `proceedFileAllocation` 全量重下（反正整文件校验不知道坏在哪，只能整下），重下完还要再校验一次（还是错的），故失败。HTTP 服务端请求计数可作硬证据（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：整文件校验失败时 aria2 为什么「清零位图」而不是「保持原状报个错」？

答案：清零位图让任务状态与磁盘实际（已证明损坏）重新一致：piece 全部缺失 → 下载调度器会重新领取所有 piece。这为 `redownload_=true` 的全量重下提供了统一的机制（复用普通下载路径），不需要为「重下」写任何特殊代码——只是位图恰好全空而已。

**练习 2**：`validateChunk` 里如果 `readDataDropCache` 返回的长度小于缓冲区大小（文件比声称的总长短），会发生什么？

答案：`currentOffset_` 只前进实际读取的长度；若文件比 totalLength 短，`currentOffset_` 永远到不了 totalLength，`finished()` 恒为 false，校验会「卡住」每轮读 0 字节。实际中不会发生——进入校验前 `processCheckIntegrityEntry` 已做 `cutTrailingGarbage`，且入口分支保证文件长度与总长匹配或由 PieceStorage 补齐（如 markAllPiecesDone 先把位图置满）。分块校验器则显式处理了「读不够」的异常（见 4.5）。

**练习 3**：为什么校验成功路径调用的是 `markAllPiecesDone()` 而不是逐块置位？

答案：整文件摘要证明的是「整个文件正确」这一个事实，没有分块信息；`markAllPiecesDone` 一次性置满位图即可表达「全部就绪」，比逐块置位便宜，也与 u5-l2 讲的位图单一真相源一致。

### 4.5 分块校验器：IteratableChunkChecksumValidator 与断点续传

#### 4.5.1 概念说明

`IteratableChunkChecksumValidator` 处理有 piece 哈希表的任务（.torrent、Metalink、以及 Metalink/HTTP 的 Link 头）。它与整文件校验器的三点本质区别：

1. **粒度**：每轮 `validateChunk` 完成**一个 piece**（内部仍按 4 KB 分片读盘），而不是 4 KB。
2. **判定输出**：不维护单个摘要，而是维护一张**本地位图** `bitfield_`——每块校验通过置位、失败清位；**全部块校验完才一次性写回** PieceStorage（中途写回会造成「校验到一半」的中间状态污染下载调度）。
3. **失败的含义**：失败不是「任务失败」而是「第 N 块缺」——写回位图后，只有缺失的 piece 会被重新领取下载。这正是 `--check-integrity=true -c` 断点续传「跳过已完成块」的机制本体。

它还显式处理了「文件比预期短」（断点续传时上次没下完）：`digest` 读不够时抛 `DL_ABORT_EX`，被 `validateChunk` 捕获后把当前块清位并继续下一块——**部分存在的文件也能被逐块评估**，已存在且正确的尾块照样通过校验。

最后一个 piece 的长度特判：总长未必是 piece 长度的整数倍，末块取 `totalLength - offset`。

#### 4.5.2 核心流程

```
init():  ctx_ = MessageDigest::create(piece 哈希类型); bitfield_->clearAllBit(); currentIndex_ = 0
loop:
  validateChunk():
    try:
      actual = calculateActualChecksum()      # 末块取剩余长度，其余取 pieceLength
      actual == pieceHashes[currentIndex_] ? bitfield_->setBit(index)
                                          : (记日志 + unsetBit(index))
    catch 读盘异常:  unsetBit(index)           # 文件不够长：该块视为缺失
    ++currentIndex_
    if currentIndex_ == numPieces:             # 全部块完成
        pieceStorage_->setBitfield(bitfield_)  # 一次性写回真实进度
之后：
    PieceStorage 按「校验后的位图」继续调度 → 只下载缺失块（StreamCheckIntegrityEntry::onDownloadIncomplete）
```

#### 4.5.3 源码精读

- [src/IteratableChunkChecksumValidator.cc:57-66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L57-L66) —— 构造时即用「piece 长度 × 总长」new 出本地 `BitfieldMan`；`currentIndex_` 是游标。
- [src/IteratableChunkChecksumValidator.cc:70-104](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L70-L104) —— `validateChunk`：算当前块实际摘要、比对、置位/清位；失败时用 `EX_INVALID_CHUNK_CHECKSUM` 格式打出「块号 + 偏移 + 期望/实际」（断点续传实践中的关键观察行）；读盘 `RecoverableException` 被捕获后清位继续（注释：Some part of file may be missing. Continue operation.）；**只有 `finished()` 时才 `setBitfield` 写回**。
- [src/IteratableChunkChecksumValidator.cc:106-118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L106-L118) —— `calculateActualChecksum`：末块长度特判 `currentIndex_ + 1 == getNumPieces()`。
- [src/IteratableChunkChecksumValidator.cc:120-125](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L120-L125) —— `init`：按 **piece 哈希类型**（与整文件哈希类型是两个字段）创建 MessageDigest、清位图、游标归零。
- [src/IteratableChunkChecksumValidator.cc:127-145](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L127-L145) —— `digest(offset, length)`：单个 piece 的读取循环。先 `ctx_->reset()`（**对象复用**，一次创建 N 次重置，省去 N 次查表与上下文分配）；4 KB 分片读 `readDataDropCache`；读到 0（数据不够长）抛 `EX_FILE_READ ... data is too short`——这就是被上层捕获的「文件短缺」信号。
- [src/IteratableChunkChecksumValidator.cc:147-160](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L147-L160) —— `finished()`（游标 ≥ piece 数）与 `getCurrentOffset()`（`index × pieceLength`，供进度条换算）。

配套的测试可以当作行为规格读：

- `test/IteratableChunkChecksumValidatorTest.cc` 与 `test/IteratableChecksumValidatorTest.cc`（配合 `test/chunkChecksumTestFile250.txt`）覆盖了通过、失败、文件短缺等分支。

#### 4.5.4 代码实践

**实践目标**：演示「分块校验 + 断点续传 → 只重下坏块」。分块校验需要 piece 哈希，HTTP 直链默认没有，因此用一个 .torrent（或 .meta4）任务来做。

**操作步骤**：

1. 准备一个大一点的文件（如 8 MB）并用 `mktorrent -l 256`（piece 长度 256 KB → 32 个 piece）或 `transmission-create` 生成种子，在本地起 trackerless 环境（`aria2c --enable-dht` + 一个做种客户端）或使用 `--seed-ratio=0` 的本机做种。没有现成工具时，可改用带 piece 哈希的 `.meta4` 文件（`.meta4` 的 `<hash piece>` 元素同样填入 pieceHashes_）。
2. 先完整下载一次，确认成功。
3. 篡改已下载文件中部的一小段（模拟磁盘损坏）：

   ```bash
   # 把第 2 MB 处的 16 字节改成 0
   dd if=/dev/zero of=<已下载文件> bs=1 seek=2000000 count=16 conv=notrunc
   ```

4. 删除 `.aria2` 控制文件（让 aria2 只能靠校验发现损坏），带校验重启续传：

   ```bash
   ./src/aria2c --check-integrity=true -c -d <目录> <种子文件> \
       --log=/tmp/cs2.log --log-level=info
   ```

5. 观察日志中的 `checksum mismatch` 类条目（即 `EX_INVALID_CHUNK_CHECKSUM` 格式的行，含 piece 编号与偏移），数一数有几个 piece 报错。

**需要观察的现象**：

- 进度行先显示 `[Checksum:...]` 从 0% 走到 100%（校验阶段），随后进入下载阶段，但**下载量远小于整个文件**——只有被篡改波及的 1 个 piece（256 KB）及重新领取的开销。
- 日志里恰好有 1 条块校验失败记录，piece 编号 ≈ 2000000 ÷ 262144 ≈ 第 7 块（0 起数）；其余 31 块无失败记录、被位图直接标记完成。
- 对照源码 [src/IteratableChunkChecksumValidator.cc:99-101](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/IteratableChunkChecksumValidator.cc#L99-L101)：写回的位图里只有该块为 0，下载调度只领取该块。

**预期结果**：分块校验把「重下 8 MB」缩小为「重下 256 KB」，这就是 torrent 续传高效的原因。本实践依赖本地搭建 tracker/做种环境，具体数值待本地验证；若环境受限，可直接阅读 `test/IteratableChunkChecksumValidatorTest.cc` 中的断言（构造部分完成的位图 + 篡改块 → 期望只有对应位被清）作为「源码阅读型实践」替代。

#### 4.5.5 小练习与答案

**练习 1**：分块校验器为什么「全部块完成后才写回位图」，而不是每校验一块就更新一次 PieceStorage？

答案：PieceStorage 的位图是下载调度的真相源（u5-l2）。若边校验边写回，校验进行到一半时位图会呈现「部分完成」的中间态，下载命令可能据此开始领取「尚未校验但暂时通过」的块，与校验循环并发读写同一文件区域，状态混乱。攒齐后一次 `setBitfield` 是原子交接，且只触发一次调度刷新。

**练习 2**：`digest()` 开头为什么要 `ctx_->reset()`？这与整文件校验器的用法有何不同？

答案：同一个 MessageDigest 对象被 N 个 piece 复用，每次算新块前必须把哈希状态归零，否则会把前一块的数据混进后一块的摘要。整文件校验器则是「一个对象从头喂到尾取一次摘要」，只需 init 时创建一次、无需中途 reset。两种用法展示了流式哈希的两种典型节奏。

**练习 3**：一个 100 MB 文件、piece 长度 1 MB，第 50 块（0 起数）校验失败被重下。假设重下后该块通过，aria2 还会再校验其余 99 块吗？

答案：不会。分块校验发生在任务入口（断点续传场景）或 BT 本地校验场景；重下的块在写盘时由 piece 级写路径即时校验（piece 哈希在下载过程中由 Piece/Segment 完成时验证，见 u5-l2 的 completePiece 链路），其余块位图已置位即视为完成，不再重复全量校验。

## 5. 综合实践

**任务**：写一份「aria2 校验行为矩阵」实验报告，把本讲四个模块串起来。

设计并执行以下 6 组实验（全部用 4.1.4 的本地 HTTP 服务与测试文件，BT 组用 4.5.4 的种子）：

| 组 | 场景 | 关键选项 | 预期退出码 | 预期是否重下 |
| --- | --- | --- | --- | --- |
| 1 | HTTP + 正确 sha-256 | `--checksum` | 0 | 无需 |
| 2 | HTTP + 错误 sha-256（新下载） | `--checksum` | 32 | 否（redownload_=false） |
| 3 | HTTP + 错误 sha-256 + 文件已存在 | `--checksum --check-integrity -c` | 32 | **是**（redownload_=true，仍失败） |
| 4 | HTTP + 无校验和 | 无 | 0 | ——（无可校验，直接成功） |
| 5 | BT + 篡改 1 块 | `--check-integrity -c` | 0 | 仅坏块 |
| 6 | 任意 + `--hash-check-only` | `--check-integrity --hash-check-only` | 视结果 | 否（只校验不下载） |

执行要点：

1. 每组记录：退出码（`echo $?`）、日志中的关键行（`Checksum validation failed`、`EX_INVALID_CHUNK_CHECKSUM` 格式行、`[Checksum:...]` 进度）、HTTP 服务端请求次数。
2. 为每一组标注它走的源码路径：入口在 `createCheckIntegrityEntry` 的哪个分支（[src/RequestGroup.cc:221-269](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/RequestGroup.cc#L221-L269)）或 `DownloadCommand::prepareForNextSegment`（[src/DownloadCommand.cc:317-338](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/DownloadCommand.cc#L317-L338)）；用的是哪个校验器；失败处置走的哪个 onDownloadIncomplete 分支。
3. 回答收尾问题：如果让你给「校验失败自动换镜像重下」写一份改动方案，应该改 `ChecksumCheckIntegrityEntry::onDownloadIncomplete` 的哪一段？需要哪些额外信息（提示：redownload_ 分支已经演示了「回到下载阶段」的路径，换镜像还需要重置 FileEntry 的 URI 尝试状态）。

（涉及 BT 的第 5 组需要本地搭建做种环境；无法搭建时以阅读 `test/IteratableChunkChecksumValidatorTest.cc` 的断言替代，并在报告中注明。）

## 6. 本讲小结

- aria2 的校验数据分两级：`Checksum`（整文件摘要，来自 `--checksum`/Metalink）与 `ChunkChecksum`（分块哈希表，来自 .torrent/Metalink），最终都沉淀到 `DownloadContext` 的四个字段；「可校验/需校验」由 `isChecksumVerificationAvailable` 等三个判定函数把关。
- `MessageDigest` 是 pImpl 门面 + 静态注册表：五个后端（OpenSSL/libgcrypt/libnettle/Apple/内置）在编译期由 configure 择一链接，各自实例化 `MessageDigestImpl::hashes` 表；运行期 `create` 只是一次查表，能力查询（supports/isValidHash/isStronger）全部同源。
- 校验执行遵循 `IteratableValidator` 契约（init → 循环 validateChunk → finished），由 RealtimeCommand 型 `CheckIntegrityCommand` 每轮推进一小步，`CheckIntegrityMan`（SequentialPicker 单槽停车场）保证全局串行；进度经 ConsoleStatCalc 以 `[Checksum:...]` 呈现。
- 整文件校验器失败即「位图清零」，是否全量重下由入口处的 `redownload_` 标志决定：普通下载完成后的校验失败只报 `CHECKSUM_ERROR`(32)，断点续传/文件已存在场景则全量重下。
- 分块校验器逐 piece 比对、本地位图攒结果、完成后一次性写回；文件短缺的块清位跳过。配合 `--check-integrity=true -c`，断点续传只重新下载损坏的 piece——这就是「校验与位图配合跳过已完成块」的完整机制。
- 入口选择集中在 `RequestGroup::createCheckIntegrityEntry`（四分支）与 `processCheckIntegrityEntry`（三条件闸门），下载完成后的补验入口在 `DownloadCommand::prepareForNextSegment`。

## 7. 下一步学习建议

- **u6-l11（会话恢复与 .aria2 控制文件）**：本讲的 `--check-integrity -c` 场景依赖控制文件里的已存位图；下一讲逆向 `DefaultBtProgressInfoFile` 如何把位图写进 .aria2、`BtCheckIntegrityEntry`（PieceHashCheckIntegrityEntry 的 BT 子类）如何在恢复时衔接 BT 运行时装配，两讲合起来就是断点续传的全貌。
- **u8-l1/u8-l2（Metalink）**：看 `ChunkChecksum` 对象的「生前」——Metalink 解析状态机如何逐元素攒出 piece 哈希表，以及多镜像场景下 `HashTypeStronger`/`isStronger` 如何挑选最强哈希。
- **延伸阅读**：`src/message_digest_helper.cc` 的 11 个调用点（`grep -n "message_digest::digest" src/`）是很好的「哈希在协议中如何被使用」的索引——torrent infohash、DHT token、MSE 握手密钥都基于同一门面，可作为 u6/u7 的预习材料。
