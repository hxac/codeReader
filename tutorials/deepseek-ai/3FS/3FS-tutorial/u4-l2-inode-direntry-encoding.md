# Inode 与 DirEntry 的 KV 编码

## 1. 本讲目标

在上一讲（u4-l1）里我们已经知道：meta 服务自己是**无状态**的，所有文件元数据都躺在 FoundationDB（FDB）这个事务型 KV 存储里。也就是说，整个文件系统其实是一张张 `(key, value)` 对。那么：

- 一个 inode（文件/目录/符号链接）在 FDB 里长什么样？
- 一个目录项（direntry，即"父目录下的某个名字 → 某个 inode"）在 FDB 里长什么样？
- 为什么 3FS 的 inode id 要用**小端序（little-endian）**编码进 key？这看似反直觉的设计能带来什么好处？
- 为什么列出目录（`ls`）在 3FS 里是一件很便宜的事？

读完本讲，你将能够：

1. 看懂 `INOD`、`DENT` 两类 key 的字节级布局，并能手工拼出一个具体的 key。
2. 说清楚 inode id 采用小端序编码、从而把写入"摊薄"到多个 FDB 存储节点上的原理。
3. 理解"同一目录下的所有目录项在 key 空间里天然连续"，从而能用一次**范围查询**（range query）高效列出目录。

## 2. 前置知识

### 2.1 FoundationDB 的有序 KV 模型

FDB 本质上是一个**按字节字典序排序**的大 KV 字典。它的两个特性贯穿本讲：

- **全局有序**：key 之间有严格的字节序，`getRange(begin, end)` 可以一次性取出一个连续区间内的所有 KV。
- **按 key 区间分片（shard）**：FDB 内部把整个 key 空间切成若干个连续区间（shard），分散到多台**存储服务器（storage server）**上。这就引出了"热点（hot spot）"问题——如果写入总是集中在某一段 key 上，那一段所在的 shard（以及它所在的那台机器）就会被写爆，吞吐被单机限制住。

> 术语：本讲里"小端序/little-endian"指**最低位字节排在最前**的存储方式（与 x86 CPU 内存里的整数表示一致）。"大端序/big-endian"则相反。

### 2.2 inode 与目录项是文件系统的两根支柱

文件系统元数据主要由两类对象构成（详见 `design_notes.md` 的 *File metadata on transactional key-value store* 一节）：

- **inode**：描述一个文件/目录/符号链接本身的属性（属主、权限、时间戳；文件还有长度、布局；目录还有父目录 id、子项默认布局）。
- **directory entry（dir entry / direntry）**：描述"某个父目录下、某个名字，指向哪个 inode"。它是把"名字树"串起来的胶水。

3FS 给每个 inode 分配一个**全局唯一、单调递增的 64 位 id**（见 [src/fbs/meta/Common.h:142](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Common.h#L142) 附近的注释，以及 design_notes 中 *"each identified by a globally unique 64-bit identifier that increments monotonically"*）。这个"单调递增"是理解第 4.2 节热点问题的钥匙。

### 2.3 key 与 value 的职责切分

3FS 的一条朴素但重要的设计：**把对象的"身份"放进 key，把"内容"放进 value**。

- inode 的 id 只在 key 里出现，value 里**不重复存 id**；
- dir entry 的 `(parent, name)` 只在 key 里出现，value 里只存"目标 inode id + 类型"。

这样做既省空间，又让"按 id 精确查找"天然就是一次 FDB 点查（point get）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/common/kv/KeyPrefix-def.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/KeyPrefix-def.h) | 集中声明所有 key 前缀（`INOD`/`DENT`/`INOS`…），避免前缀重复。 |
| [src/common/kv/KeyPrefix.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/KeyPrefix.h) | `KeyPrefix` 枚举与 `makePrefixValue` 的巧妙构造。 |
| [src/fbs/meta/Common.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Common.h) | `InodeId` 类型，含 `packKey/unpackKey`（小端序编码的核心）。 |
| [src/fbs/meta/Schema.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Schema.h) | `InodeData`/`DirEntryData`/`DirEntry` 数据结构（value 侧）。 |
| [src/meta/store/Inode.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.h) / [Inode.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.cc) | inode 的 `packKey/unpackKey/load/store`。 |
| [src/meta/store/DirEntry.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.h) / [DirEntry.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.cc) | dir entry 的 `packKey/unpackKey` 与 `DirEntryList` 的范围查询实现。 |
| [src/common/kv/ITransaction.cc](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc) | `prefixListEndKey`/`keyAfter`——把"前缀"变成"半开区间端点"的工具。 |
| [src/common/utils/SerDeser.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/SerDeser.h) | key 拼接用的裸字节序列化器 `Serializer::put/putRaw`。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**① key 前缀与整体编码格式**、**② inode id 的小端序与负载分散**、**③ DENT 的范围查询与目录列举**。

### 4.1 key 前缀与整体编码格式

#### 4.1.1 概念说明

3FS 在 FDB 里要存很多种对象（inode、目录项、session、用户、链表、配置…）。如果所有对象的 key 混在一起，既难管理，也可能因为前缀重叠而出错。于是 3FS 给每一类对象分配一个**固定 4 字节的前缀**（magic bytes），key 的第一段永远是这个前缀。前缀起到两个作用：

1. **命名空间隔离**：不同类型对象的 key 互不干扰，FDB 里大致按前缀自然分簇。
2. **可读性**：前缀被设计成 4 个 ASCII 字符（如 `INOD`、`DENT`），用工具直接 dump FDB 时一眼能认出这一段属于谁。

本讲的主角是两个前缀：

- `INOD` —— inode 的 key；
- `DENT` —— directory entry 的 key。

（你还会在 [KeyPrefix-def.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/KeyPrefix-def.h) 里看到 `INOS`（inode session）、`META`、`USER` 等，它们是其它对象的命名空间，不在本讲范围。）

#### 4.1.2 核心流程

两类 key 的总体拼装流程：

```text
inode key   =  "INOD"(4B)  +  inode_id 小端序(8B)
dir entry key = "DENT"(4B)  +  parent_inode_id 小端序(8B)  +  name(变长原始字节)
```

注意三个要点：

- **前缀固定 4 字节**，由 `KeyPrefix` 枚举统一管理，编译期就杜绝了前缀重复。
- **inode id 恒为 8 字节定长**（`InodeId::Key = std::array<uint8_t, 8>`），所以 inode key 长度恒为 12 字节，点查非常规整。
- **dir entry key 是变长**（名字长度可变），但它的前 12 字节（`DENT + parent`）是定长的公共前缀——这一点是第 4.3 节范围查询的基础。

对应的 value 侧：

- inode value = `serde::serialize(InodeData)`（类型变体 + acl + nlink + 三个时间戳；文件还含 length/layout，目录还含 parent/默认 layout/name）。**不含 inode id**。
- dir entry value = `serde::serialize(DirEntryData)`（目标 inode id + 类型 + 可选 dirAcl/uuid/gcInfo）。**不含 (parent, name)**，因为它们已经在 key 里了。

#### 4.1.3 源码精读

**(a) 前缀的集中声明。** 所有前缀在一个 X-macro 文件里声明，避免散落各处导致重复：

[src/common/kv/KeyPrefix-def.h:6-7](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/KeyPrefix-def.h#L6-L7) —— 本讲的两个主角 `INOD` 与 `DENT`。

**(b) 前缀值的巧妙构造。** 前缀虽然逻辑上是 4 个字符，但代码里它是一个 `uint32_t` 枚举值。关键在 `makePrefixValue` 怎么把字符串变成整数：

[src/common/kv/KeyPrefix.h:8-19](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/KeyPrefix.h#L8-L19) —— `makePrefixValue` 把 `s[0]` 放在最低位、`s[3]` 放在最高位。

为什么这样摆？因为 3FS 跑在 x86（小端机）上，一个 `uint32_t` 在内存里本来就是"最低位字节在最前"。把 `'I'` 放在最低位，意味着写到 FDB 的 4 个字节恰好按 `'I','N','O','D'` 的顺序排列——也就是说，**序列化到磁盘上的前缀字节，肉眼读起来正好就是 "INOD"**。我们会在 4.1.4 用字节级例子验证这一点。

**(c) inode key 的拼装。** 头文件里的注释一句话说清了格式：

[src/meta/store/Inode.h:48-51](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.h#L48-L51) —— `key format: kInodePrefix + InodeId.key`。

实现就是把前缀和 `InodeId::packKey()` 的结果依次裸写进缓冲：

[src/meta/store/Inode.cc:41-45](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.cc#L41-L45) —— `packKey` 用 `Serializer::serRawArgs(prefix, inodeId)` 把两段定长数据连起来。

`unpackKey` 是逆操作，先读出前缀（并断言它确实是 `Inode`），再用 `InodeId::unpackKey` 还原 id：

[src/meta/store/Inode.cc:49-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.cc#L49-L60)。

**(d) dir entry key 的拼装。** 注释同样点明格式：

[src/meta/store/DirEntry.h:70-72](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.h#L70-L72) —— `Key format: prefix + parent-InodeId.key + name`。

实现里多了"变长 name"这一段，用 `putRaw` 把名字的原始字节直接追加（**不带长度前缀、不带分隔符**，因为 name 是 key 的最后一段，读到 key 末尾即读完名字）：

[src/meta/store/DirEntry.cc:43-52](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.cc#L43-L52)。

`unpackKey` 用 `getRawUntilEnd()` 把"前缀 + 8 字节 parent"之后的所有字节当作 name 读出来：

[src/meta/store/DirEntry.cc:56-70](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.cc#L56-L70)。

**(e) 为什么是"裸写"？** `Serializer::put` 对平凡类型（trivial）直接 `putRaw(&v, sizeof(T))`，即把内存里的原始字节倒进缓冲，不做任何字节序转换：

[src/common/utils/SerDeser.h:56-60](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/utils/SerDeser.h#L56-L60)。

这对前缀没问题（前缀的值就是按"小端机内存里读出来是 INOD"构造的）；但对 inode id 来说，"裸写"意味着 key 里出现的字节顺序，完全由 `InodeId::packKey` 决定——这就引出了第 4.2 节。

#### 4.1.4 代码实践：手工拼出两个示例 key 的字节布局

**实践目标**：用纸笔（或下面给出的示例脚本）拼出"inode id = 4096（0x1000）"和"根目录下名为 `data` 的目录项"这两个 key 的字节级布局，验证前缀确实是可读的 ASCII。

**操作步骤（手工推导）**：

1. inode key（id = 0x1000）：
   - 前缀 `INOD` → `49 4E 4F 44`（'I'=0x49,'N'=0x4E,'O'=0x4F,'D'=0x44）。
   - inode id 小端序：0x0000000000001000 → 最低位字节在前 → `00 10 00 00 00 00 00 00`。
   - 拼起来：`49 4E 4F 44 | 00 10 00 00 00 00 00 00`（共 12 字节）。

2. dir entry key（parent = root = 0，name = "data"）：
   - 前缀 `DENT` → `44 45 4E 54`。
   - parent 小端序：0 → `00 00 00 00 00 00 00 00`。
   - name "data" → `64 61 74 61`。
   - 拼起来：`44 45 4E 54 | 00 00 00 00 00 00 00 00 | 64 61 74 61`（共 16 字节）。

**需要观察的现象**：

- 前缀 4 字节确实是可读的 `INOD` / `DENT`。
- inode id 部分是**低位字节在前**（0x1000 写成 `00 10 ...` 而不是 `00 ... 10 00`）。
- dir entry 的前 12 字节 `DENT + parent` 是定长公共前缀。

**预期结果**：与上面给出的字节序列一致。

**用脚本验证（示例代码，非项目代码）**：下面这段 Python 可以快速算出小端序字节，供你核对手工结果：

```python
# 示例代码：仅供验证字节布局，不是 3FS 的一部分
import struct
inode_id = 0x1000
prefix_inode = b"INOD"
key_inode = prefix_inode + struct.pack("<Q", inode_id)   # '<' = little-endian, Q = uint64
print(key_inode.hex(" "))                                 # 49 4e 4f 44 00 10 00 00 00 00 00 00

prefix_dent = b"DENT"
parent = 0
name = b"data"
key_dent = prefix_dent + struct.pack("<Q", parent) + name
print(key_dent.hex(" "))                                  # 44 45 4e 54 00 00 00 00 00 00 00 00 64 61 74 61
```

> 待本地验证：如果你手头有 FDB 实例，dump 出对应区间，前 4 字节应当与上述一致；若暂无环境，纸笔推导 + 脚本核对即可。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `makePrefixValue` 改成 `s[3] + (s[2]<<8) + (s[1]<<16) + (s[0]<<24)`（即按大端位权摆放），在同样的 x86 小端机上，写到 FDB 的前缀字节会变成什么样？还能读出 "INOD" 吗？

**答案**：新写法把 `'D'` 放在最低位。小端机内存里这个 `uint32_t` 的字节顺序会变成 `'D','O','N','I'`。写到 FDB 就是 `DNIO`——**读不出 "INOD" 了**。这正说明现有 `makePrefixValue` 的摆位是特意为小端机设计的。

**练习 2**：dir entry 的 name 直接以裸字节接在 key 末尾、且不带长度前缀。如果允许 name 里出现任意字节（包括 `\x00`），`unpackKey` 还能正确还原 name 吗？

**答案**：能。因为 `unpackKey` 用 `getRawUntilEnd()` 读取"parent 之后直到 key 结束"的全部字节作为 name——key 本身的长度就隐含了 name 的长度，name 里有没有 `\x00` 都无所谓。FDB 的 key 是任意二进制串，不把 `\x00` 当结束符。

---

### 4.2 inode id 的小端序与负载分散

#### 4.2.1 概念说明

这是本讲最"反直觉"但最关键的设计。直觉上，把一个整数编码进有序 KV 的 key，**大端序**更自然——因为大端序下，整数的大小顺序与 key 的字节字典序一致（id 小的 key 排前面），便于按 id 范围扫描。

但 3FS 偏偏用**小端序**。源码注释一句话道破原因：

[src/fbs/meta/Common.h:134-135](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Common.h#L134-L135) —— *"Use little endian form as key in FoundationDB, it helps to avoid hot spot"*（用小端序做 FDB 的 key，有助于避免热点）。

`design_notes.md` 也明确写道：*"Inode keys are constructed by concatenating the 'INOD' prefix with the inode id, which is encoded in little-endian byte order to spread inodes over multiple FoundationDB nodes."*（inode id 以小端序编码，目的是把 inode 摊开到多个 FDB 节点上）。

要理解这句话，必须把"inode id 单调递增"和"FDB 按 key 区间分片"这两件事放在一起看。

#### 4.2.2 核心流程

先看结论，再看为什么：

```text
单调递增的 inode id  +  大端序 key  ⇒  所有新 inode 挤在 key 空间一端  ⇒  写热点（单 shard 扛全部写入）
单调递增的 inode id  +  小端序 key  ⇒  最低位字节变成 key 首字节，随分配自然循环 0x00..0xFF
                                  ⇒  连续分配的 inode 被摊薄到 ~256 个 shard 上  ⇒  写入随机器规模水平扩展
```

**大端序为什么会热点？** 假设连续分配 id = 4096(0x1000), 4097, 4098 ……：

| inode id | 大端序 key 的 inode 部分（8B） | 相邻 key 差异 |
| --- | --- | --- |
| 0x1000 | `00 00 00 00 00 00 10 00` | — |
| 0x1001 | `00 00 00 00 00 00 10 01` | 仅最后 1 字节变 |
| 0x1002 | `00 00 00 00 00 00 10 02` | 仅最后 1 字节变 |

这些 key 共享 7 个前导字节，彼此紧挨着，且**所有未来分配的新 id 都接在它们后面**（因为 id 只增不减）。于是"当前写前线"永远是 key 空间里那一段不断向右增长的连续区间——它整段落在**同一个 shard、同一台存储机器**上。这台机器成为写入瓶颈，且随着 id 增长不断触发 shard 分裂，是典型的顺序写热点。

**小端序如何破局？** 同样这几个 id，小端序下（最低位字节排到最前）：

| inode id | 小端序 key 的 inode 部分（8B） | key 首字节 |
| --- | --- | --- |
| 0x1000 | `00 10 00 00 00 00 00 00` | 0x00 |
| 0x1001 | `01 10 00 00 00 00 00 00` | 0x01 |
| 0x1002 | `02 10 00 00 00 00 00 00` | 0x02 |
| … | … | … |
| 0x10FF | `FF 10 00 00 00 00 00 00` | 0xFF |
| 0x1100 | `00 11 00 00 00 00 00 00` | 0x00（回到开头附近） |

相邻 id 现在在**第一字节**上变化。每分配 256 个连续 id，key 的首字节就把 `0x00..0xFF` 走了一遍，于是这批 inode 被**均匀地撒到整个 key 空间**，落到约 256 个不同的 shard、即多台存储机器上。原本由 1 台扛的写入，现在由数百个 shard 分担。

如果用概率的语言近似描述：把 id 的最低字节看作近似均匀分布的随机量 \(B_0\)，则 key 的首字节分布为

\[
P(\text{首字节}=b) \approx \frac{1}{256},\quad b\in[0,255]
\]

写入因此近似均匀地落在按首字节划分的各 shard 上，单 shard 写入压力被压到原来的约 \(1/256\)。这就是"spread inodes over multiple FoundationDB nodes"的数学含义。

**代价是什么？** 小端序下，id 的数值大小与 key 字典序**不再一致**——你无法用一个连续 range 高效扫描"id 从 4096 到 8192 的所有 inode"。但 meta 服务**从不需要这种扫描**：inode 永远是按精确 id 点查（`get(packKey(id))`），从不按 id 区间遍历。所以这个代价在 3FS 的访问模式下为零，纯属白赚。

#### 4.2.3 源码精读

`InodeId` 把上面这套逻辑封装在 `packKey/unpackKey` 两个方法里：

[src/fbs/meta/Common.h:175-182](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Common.h#L175-L182) —— `packKey` 先用 `folly::Endian::little` 把 `uint64_t` 转成小端整数，再 `bit_cast` 成 8 字节数组；`unpackKey` 反过来 `bit_cast` 回整数再做一次小端转换。

注意 `Key = std::array<uint8_t, 8>` 是**定长**的（[Common.h:135](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Common.h#L135)）。这一点配合 4.1 节的 `serRawArgs`/`put`，保证了 inode key 恒为 12 字节，无任何变长成分。

inode 的读取路径分两档，区别仅在"是否把 key 加入读冲突范围"，与字节序无关，但值得一看，因为它体现了上一讲（u2-l6）的快照读/冲突读语义：

[src/meta/store/Inode.cc:62-84](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.cc#L62-L84) —— `loadImpl` 用函数指针在 `snapshotGet`（快照读，不进冲突范围）与 `get`（冲突读）之间二选一，然后用 `packKey(id)` 点查。

[src/meta/store/Inode.cc:105-141](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/Inode.cc#L105-L141) —— `store` 用 `packKey()` 生成 key、`serde::serialize(data())` 生成 value，`txn.set(key, value)` 写入。注意 value 来自 `data()`（即 `InodeData`），**不含 id**——id 只来自 key。

#### 4.2.4 代码实践：观察"摊薄"效应

**实践目标**：用一个最小脚本（示例代码）生成一段连续 id 的大端/小端 key，肉眼对比它们的分布，直观感受小端序如何把连续 id 打散。

**操作步骤**：

```python
# 示例代码：对比连续 id 在大端/小端下的 key 分布
import struct
prefix = b"INOD"
for i in range(0x1000, 0x1000 + 8):
    be = prefix + struct.pack(">Q", i)   # 大端
    le = prefix + struct.pack("<Q", i)   # 小端
    print(f"id={i:#06x}  BE={be.hex(' ')}   LE={le.hex(' ')}")
```

**需要观察的现象**：

- 大端那一列，8 条 key 的前 7 字节几乎完全相同，只在末字节递增——它们会挤在一起。
- 小端那一列，第 5 字节（首字节，即 `INOD` 之后第一个字节）从 `00` 递增到 `07`——8 条 key 已经散开。

**预期结果**：把小端列的 key 排序后，相邻 id 的 key 之间间隔很大；继续把 `range` 扩到 256，会看到首字节扫过整个 `0x00..0xFF`，对应 256 个不同的"桶"。

> 待本地验证：在有 FDB 的环境里，向同一前缀连续写入 256 个 inode 并观察 storage server 的写入分布；无环境时用脚本即可看到 key 的散布。

#### 4.2.5 小练习与答案

**练习 1**：如果把 inode id 改回大端序，meta 服务的哪个**正确性**功能会立刻坏掉？哪个**性能**问题会出现？

**答案**：正确性不会坏——`packKey/unpackKey` 是对称的，只要编解码一致，点查照样工作（key 不需要"按数值有序"）。真正出现的是**性能**问题：连续分配的新 inode 全部堆在 key 空间一端，形成写热点，单个 FDB storage server 被打满，集群写吞吐被单机限制。

**练习 2**：除了 inode id，3FS 里还有什么场景"按单调递增整数生成 key 却又必须避免热点"？你会用什么思路？

**答案**：思路一致——把"变化最快的位"挪到 key 最高有效位置。常见手段就是**字节反转 / 小端序**（本讲的做法），或给 key 加一个**哈希前缀**。3FS 在文件长度更新任务里就用了另一种手段——按 inode id 做 **rendezvous 哈希**把任务分散到多个 meta 实例（见 design_notes 与 u4-l5），同样是出于"避免单点热点"的动机。

---

### 4.3 DENT 的范围查询与目录列举

#### 4.3.1 概念说明

`ls` 一个目录，本质是"找出这个目录下的所有目录项"。如果每找一项都要一次点查，大目录会非常慢。3FS 的 dir entry key 设计让"同一目录下的所有项"在 key 空间里**天然连续**，于是列出目录只需要**一次范围查询**。

回顾 4.1 的 key 格式：

```text
dir entry key = "DENT" + parent(小端 8B) + name(变长)
```

所有 `parent` 相同的目录项，前 12 字节（`DENT + parent`）完全一样，只有后面的 name 不同。在 FDB 的字节字典序里，它们必然排成一个连续区间。这段公共前缀正是范围查询的"锚点"。

`design_notes.md` 对此的总结是：*"All entries within a directory naturally form a contiguous key range, allowing efficient directory listing via range queries."*

#### 4.3.2 核心流程

列出 `parent` 目录下所有项（分页）的流程：

```text
1. 计算公共前缀  prefix = "DENT" + parent(小端 8B)
2. 计算区间端点  endKey = prefixListEndKey(prefix)   # 把前缀末字节 +1（处理 0xFF 进位）
3. 范围查询      range = ( packKey(parent, prev) ,  endKey )   # prev 是游标，初值为 ""
4. FDB 返回该区间内的 KV，逐个 unpackKey 出 (parent, name)、deserialize 出目标 inode id/类型
5. 若命中 limit，记录最后一条 name 作为下一页的 prev；否则结束
```

关键在 `prefixListEndKey`：它把一个前缀转换成"严格大于该前缀下所有 key 的最小 key"，从而把"前缀匹配"变成一个标准的**半开区间** `(prefix, endKey)`。配合 `prev` 游标，就能稳定地分页（即使目录在列举过程中被并发修改，游标也能基于 name 续上）。

`prefixListEndKey` 的算法是"从后往前找到第一个非 `0xFF` 的字节并 +1，丢弃其后所有字节"——这正是把字典序意义下的"前缀之后"表达成一个具体 key 的标准做法（类比"把字符串当作大端数加 1"，遇到 0xFF 就进位）。

#### 4.3.3 源码精读

**(a) 前缀端点工具。** `prefixListEndKey` 与 `keyAfter` 都在事务辅助层：

[src/common/kv/ITransaction.cc:42-55](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc#L42-L55) —— `prefixListEndKey` 自尾向前找第一个非 `0xFF` 字节并 `++c`，弹出尾部 `0xFF`。同文件 [L33-L40](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/common/kv/ITransaction.cc#L33-L40) 的 `keyAfter` 则是简单地在末尾追加一个 `\x00`，二者都是把"位置"变成"开区间端点"。

**(b) DirEntryList 的范围读取。** 以"按 prev 游标分页 + 快照读"的重载为例：

[src/meta/store/DirEntry.cc:276-288](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.cc#L276-L288) —— 这里能完整看到三步：`beginKey = packKey(parent, prev)`、`prefix = packKey(parent, "")`、`endKey = prefixListEndKey(prefix)`，然后用两个 `KeySelector` 圈出区间交给 `loadImpl`。

注：`prev` 初值为 `""` 时，`beginKey = packKey(parent, "")` 就是裸前缀 `DENT + parent`；begin 选择器用 `inclusive=false`（排他），正好从"前缀之后"开始，跳过那个不存在的空名字锚点，取到第一条真实目录项。

**(c) 真正调用 FDB range 的地方。**

[src/meta/store/DirEntry.cc:222-245](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.cc#L222-L245) —— `loadImpl` 用 `snapshotGetRange/getRange` 一次性取回区间内所有 KV，循环用 `DirEntry::newUnpacked(key, value)` 还原每一条目录项，并断言还原出的 `parent` 与查询的 `parent` 一致（否则 FATAL，因为区间不该跨父目录）。

**(d) 顺带：目录项的写入。** `DirEntry::store` 同样是 `packKey()` 当 key、`serde::serialize(data())` 当 value：

[src/meta/store/DirEntry.cc:144-166](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/meta/store/DirEntry.cc#L144-L166)。注意它在写入前用 `checkName` 拒绝 `.`、`..`、含 `/` 的非法名字，并用 `NAME_MAX`（来自 `<linux/limits.h>`，255）限制名字长度。

#### 4.3.4 代码实践：画出范围查询的区间边界

**实践目标**：为"列出根目录（parent=0）下的目录项"手工算出 `prefix` 与 `endKey`，确认该区间恰好覆盖根目录的所有项、且不串到别的父目录。

**操作步骤**：

1. `prefix = packKey(0, "") = DENT + parent(小端 8B)`
   = `44 45 4E 54 | 00 00 00 00 00 00 00 00`（共 12 字节）。
2. `endKey = prefixListEndKey(prefix)`：从尾向前，最后一字节是 `0x00`（非 0xFF），`++` 成 `0x01`：
   = `44 45 4E 54 | 00 00 00 00 00 00 00 01`。
3. 区间 = `(prefix, endKey)`。

**需要观察的现象**：

- 根目录下任意一项的 key 形如 `44 45 4E 54 | 00 00 00 00 00 00 00 00 | <name>`，它们都 > `prefix`（因为多了 name 字节）且 < `endKey`（因为 parent 部分仍是 `00…00`，而 `endKey` 的 parent 部分已是 `00…01`）。
- 父目录 id=1 的项 key 是 `…| 01 00 00 00 00 00 00 00 | …`（parent 小端后首字节为 `01`），它**大于** `endKey`（`00…01` vs `01 00…`，逐字节比：第 5 字节 `00` < `01`），所以**不会被误纳入**根目录的区间。

**预期结果**：区间精确覆盖 parent=0 的所有目录项，与 parent≠0 的项干净隔离。这就是"一次 range 即可 ls"的几何保证。

**用脚本核对（示例代码）**：

```python
# 示例代码：核验不同父目录的 key 是否落在各自的区间内
import struct
def dent_key(parent, name=b""):
    return b"DENT" + struct.pack("<Q", parent) + name

prefix_root = dent_key(0)                      # 根目录前缀
end_root = prefix_root[:-1] + bytes([prefix_root[-1] + 1])  # 末字节 +1（此处无 0xFF 进位）
print("range:", prefix_root.hex(" "), "->", end_root.hex(" "))

k_in_root  = dent_key(0, b"data")
k_in_one   = dent_key(1, b"data")
print("root/data 属于区间?", prefix_root < k_in_root < end_root)   # True
print("dir1/data 误入区间?", prefix_root < k_in_one  < end_root)   # False
```

> 待本地验证：有 FDB 环境时可对真实集群的 `DENT` 前缀做 `getRange` 观察；无环境时上面的字节比较即可证明隔离性。

#### 4.3.5 小练习与答案

**练习 1**：`prefixListEndKey` 为什么要"从后往前找第一个非 `0xFF` 字节再 +1、并丢弃其后的字节"，而不是简单地把前缀末字节 +1？

**答案**：因为末字节可能是 `0xFF`，简单 +1 会溢出回 `0x00`，得到一个**更小**的 key，区间就错了。正确做法是处理进位：遇到 `0xFF` 就弹出它、向前进一位。这等价于"把前缀当作一个大端数做 +1"。对于 parent=0 这种末字节为 `0x00` 的常见情形，两种写法结果相同；但通用实现必须处理 `0xFF` 进位。

**练习 2**：如果 dir entry key 里 name 排在 parent **之前**（即 `DENT + name + parent`），范围查询列目录还能高效吗？

**答案**：不能。那样的话同一目录的项不再共享一个定长前缀（因为 name 变长且排在中间），它们在 key 空间里会被 name 的字典序打散、与别的父目录的项交错，无法用一个连续 range 取出。当前 `DENT + parent + name` 的顺序正是为了让"父目录"成为定长公共前缀，是范围列目录的关键。

---

## 5. 综合实践

把三个模块串起来，完成一次"纸面元数据追踪"：

**任务背景**：假设 FDB 里此刻有这些对象——根目录 inode id=0；根目录下有一个子目录 `data`（inode id=4096=0x1000）；`data` 目录下有两个文件 `a.bin`（id=0x1001）与 `b.bin`（id=0x1002）。

**请完成**：

1. 写出根目录 inode、`data` 目录 inode 的 **INOD key**（字节级）。
2. 写出 `data` 这条目录项（在根目录下）、`a.bin`/`b.bin`（在 `data` 下）三条 **DENT key**（字节级）。
3. 计算"列出 `data` 目录"所需的 `prefix` 与 `endKey`，说明为什么 `a.bin`、`b.bin` 两条 DENT 都落在这个区间里。
4. 解释：如果此时又新建了 id=0x1003 的文件 `c.bin`，它的 INOD key 与 0x1002 的 INOD key 在 FDB 里是否相邻？这说明了 4.2 节的什么结论？

**参考要点（请先自己做再对照）**：

1. 根 inode key：`49 4E 4F 44 | 00 00 00 00 00 00 00 00`；`data` inode key：`49 4E 4F 44 | 00 10 00 00 00 00 00 00`（0x1000 小端）。
2. `data`(在 root 下)：`44 45 4E 54 | 00 00 00 00 00 00 00 00 | 64 61 74 61`；
   `a.bin`(在 data 下)：`44 45 4E 54 | 00 10 00 00 00 00 00 00 | 61 2E 62 69 6E`；
   `b.bin`(在 data 下)：`44 45 4E 54 | 00 10 00 00 00 00 00 00 | 62 2E 62 69 6E`。
3. `prefix = 44 45 4E 54 | 00 10 00 00 00 00 00 00`，`endKey = 44 45 4E 54 | 00 10 00 00 00 00 00 01`（末字节 0x00→0x01）。`a.bin`/`b.bin` 的 key 都以该 12 字节前缀开头、且 name 非空，故 `prefix < key < endKey` 成立。
4. 0x1003 的小端 key = `49 4E 4F 44 | 03 10 00 00 00 00 00 00`，与 0x1002（`… | 02 10 00 …`）在第 5 字节（首字节）相差 1，二者**不相邻**——它们落在不同的 FDB shard 上。这正是 4.2 节"小端序把单调递增 id 摊薄、避免写热点"的直接体现。

## 6. 本讲小结

- 3FS 把每类对象的 key 都冠以**固定 4 字节前缀**（inode=`INOD`、direntry=`DENT`），用 `KeyPrefix` 枚举集中管理；前缀值经 `makePrefixValue` 构造，使其在小端机内存里恰好是可读 ASCII。
- inode key = `INOD + inode_id(小端 8B)`，定长 12 字节，按 id **点查**；dir entry key = `DENT + parent_id(小端 8B) + name(变长)`，name 以裸字节接在末尾、不带长度前缀。
- **key 存身份、value 存内容**：inode id 只在 key 里；dir entry 的 (parent,name) 只在 key 里，value 只放目标 inode id+类型。
- inode id 采用**小端序**，目的是把单调递增的 id 摊薄到约 256 个 shard、避免 FDB 写热点；代价（不能按 id 区间扫描）在 meta 的点查访问模式下不存在。
- 同一目录的所有 dir entry 因共享 `DENT + parent` 定长前缀而**天然连续**，可用 `prefixListEndKey` 圈成半开区间，一次范围查询即可高效列目录，并用 `prev` 游标稳定分页。

## 7. 下一步学习建议

- **衔接 u4-l3《用 FoundationDB 事务实现元数据操作》**：本讲只讲了 key/value 的静态编码；下一讲会展示 create/lookup/rename/unlink 这些操作如何在 FDB 读写事务里**使用**这些 key，并设置读/写冲突范围、在冲突时整段重试。重点看 `src/meta/store/ops/` 下的 Open/Rename/Remove/Stat。
- **延伸阅读**：[src/fbs/meta/Common.h](https://github.com/deepseek-ai/3FS/blob/22fca04564c7cc230fd8b9523b8b92864e1dad47/src/fbs/meta/Common.h) 里 `InodeId` 的各种特殊取值（`root`/`gcRoot`/`virt`/`iov`/`rmRf`…），它们解释了 3FS 如何复用 inode id 空间表达"虚拟目录"等特殊语义。
- **跨组件联系**：inode id 还会被拼进 **chunk id**（chunk id = inode id + chunk index，见 design_notes *Location of file chunks*），这部分把"元数据层"和"存储层"连起来，将在 u4-l4（文件布局与链分配）和第五单元（storage）展开。
