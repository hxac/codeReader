# Table：码表与多级索引

## 1. 本讲目标

在上一篇 u8-l2 里，我们把拼写串送进 Prism，得到了一串 `syllable_id`（音节编号）。可是用户最终要的是**汉字词条**，不是编号。本讲就来回答：

> 给定一串 `syllable_id`，librime 如何在海量词条里快速找出所有「编码 = 这串音节」的候选词？

承担这个职责的就是 `Table`（码表）。它把人类可读的 `*.dict.yaml` 编译成一张磁盘上的**多级数组索引**，运行期用 `mmap` 直接挂载，按音节序列逐级下钻，定位到词条集合。

学完本讲你应当掌握：

1. `Table` 如何用「数组索引数组」的固定深度树，把「音节序列 → 词条集合」的映射压进一个连续的二进制文件；
2. `TableQuery` 这个**有状态游标**如何沿索引逐级 `Advance`/`Backdate`，以及它为什么把头层当数组、中间层当二分查找；
3. 超过 `kIndexCodeMaxLength` 个音节的长词条为何被「拍扁」进 `TailIndex`，以及这对长词查询的影响；
4. `OffsetPtr` 自相对寻址、`Array`/`List` 定长容器、`Syllabary`/`StringTable` 字符串去重这几块序列化地基如何协同工作。

## 2. 前置知识

本讲默认你已经读过 u8-l1（词典系统总览与 `MappedFile` 基座）和 u8-l2（Prism 产出 `syllable_id`）。为照顾遗忘，这里重述三个关键概念：

- **内存映射文件（mmap）**：把磁盘文件「铺」进进程虚拟内存，用普通指针读写就等于读写文件，省去 `read`/`write` 系统调用与反序列化步骤。`Table` 的全部数据结构都直接活在映射区里。
- **音节编号（`SyllableId`）**：一个 `int32_t`，由 Prism 在构建期给每个合法音节分配的稠密整数 id（`0 .. num_syllables-1`）。它是 Table 索引的主键。
- **编码（`Code`）**：一个词条的「拼音序列」用 `vector<SyllableId>` 表示，例如「你好」=`[ni的id, hao的id]`，「中华人民共和国」有 7 个音节。

一个核心直觉先放在这里：**稠密的小整数集合适合用数组直接下标访问（O(1)），稀疏的复合键适合用有序数组 + 二分查找（O(log n)）**。Table 的多级索引正是这两种策略的分层组合。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/dict/mapped_file.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h) | 序列化地基：`OffsetPtr` 自相对指针、`Array`/`List`/`String` 定长容器、`MappedFile` 基类与 `Allocate` 分配器 |
| [src/rime/dict/table.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h) | Table 的磁盘数据模型（`Entry`/多级索引/`Metadata`）、`TableQuery` 游标、`TableAccessor` 读取器、`Table` 类声明 |
| [src/rime/dict/table.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc) | 构建（`Build*Index`）、加载（`Load`）、查询（`Query`/`QueryWords`/`QueryPhrases`）的全部实现 |
| [src/rime/dict/vocabulary.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h) | `Code::kIndexCodeMaxLength` 这个决定索引深度的常量；构建期的内存态 `Vocabulary` 树 |
| [src/rime/dict/string_table.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/string_table.h) | 基于 marisa trie 的 `StringTable`，负责词条文本与音节串的去重存储 |

> 说明：`vocabulary.h` 与 `string_table.h` 不是本讲的「最小模块」主角，但它们提供的 `kIndexCodeMaxLength` 与字符串去重机制是理解 Table 不可绕开的背景，因此一并引用。

---

## 4. 核心概念与源码讲解

### 4.1 序列化地基：MappedFile、OffsetPtr 与定长容器

#### 4.1.1 概念说明

Table 落盘后是一个 `.table.bin` 文件。运行期 `mmap` 进来后，里面是一堆**裸的 C 结构体**——没有虚表、没有 `std::string`、没有堆指针。为什么？因为堆指针（`new` 出来的地址）每次进程启动都不一样，写进文件毫无意义。

于是 librime 自造了两样东西替代标准库：

- **`OffsetPtr`**：替代裸指针，存「相对自己的偏移量」，文件搬到哪都成立；
- **`Array` / `List` / `String`**：替代 `vector`/`string`，用「长度 + 内联/偏移数据」的定长布局，可直接 `memcpy` 进映射区。

`MappedFile` 则是它们的「内存池管家」，提供 `Allocate`（按对齐分配）、`Resize`（倍增扩容）、`Find`（按绝对偏移取指针）等能力。u8-l1 已介绍过它的角色，本讲聚焦它暴露给 Table 用的那几个零件。

#### 4.1.2 核心流程

`OffsetPtr<T>` 的关键设计是「**自相对偏移**」：它存的不是目标地址，而是「目标地址 − 自己的地址」。

\[ \text{offset} = (\text{char}*)\text{target} - (\text{char}*)(\&\text{offset\_}) \]

读取时反向计算：`target = (char*)&offset_ + offset_`。这样无论文件被映射到哪个虚拟地址，只要 `OffsetPtr` 和它指向的数据在**同一次映射**里，相对位置就不变，指针永远有效。代价是：`OffsetPtr` 不能指向自己（`0` 被保留为「空指针」），见 [mapped_file.h:L20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L20) 的注释。

`Allocate<T>` 的分配策略是「**对齐 + 倍增**」：每次分配前先用 `RIME_ALIGNED` 把已用空间向上对齐到 `alignof(T)`，空间不足则把文件容量翻倍（`file_size * 2`），再以 0 填充返回新区域的指针。这保证写进文件的结构体满足对齐要求，跨平台映射不会因未对齐访问崩溃。

#### 4.1.3 源码精读

`OffsetPtr` 的全部精髓在 `get()`，它把「自相对偏移」还原成真实地址：

[自相对指针的取值实现 src/rime/dict/mapped_file.h:L40-L44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L40-L44) —— 注意它以 `&offset_`（字段自己的地址）为基准加上 `offset_`，offset 为 0 时返回 `NULL`，这就实现了「可序列化的空指针」。

两类容器的差异要记牢：

[Array：长度 + 内联数组 src/rime/dict/mapped_file.h:L60-L68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L60-L68) —— `T at[1]` 是 C 里常见的「柔性数组」技巧，实际分配时会按 `sizeof(Array<T>) + sizeof(T)*(n-1)` 多要空间（见 `CreateArray`），于是数组元素紧跟在 `size` 字段之后、内存连续。

[List：长度 + 偏移指针 src/rime/dict/mapped_file.h:L70-L78](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L70-L78) —— 与 `Array` 唯一的区别是数据不内联，而是挂在 `OffsetPtr<T> at` 上。`Array` 用于「定长、就地」，`List` 用于「长度运行期才定、或想复用同一段数据」。

`Allocate` 的对齐与倍增：

[Allocate 模板：对齐后分配，不足则倍增扩容 src/rime/dict/mapped_file.h:L134-L152](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L134-L152) —— `RIME_ALIGNED(size_, T)` 把当前已用空间向上取整到 `T` 的对齐倍数；新容量取「所需空间」与「旧容量 ×2」的较大值，避免频繁扩容。

#### 4.1.4 代码实践

**目标**：用纸笔验证「自相对偏移」确实与映射地址无关。

**操作步骤**：

1. 阅读 [mapped_file.h:L40-L51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L40-L51) 的 `get()` 与 `to_offset()`。
2. 假设某 `OffsetPtr<char>` 字段位于映射区起始地址 `base + 100`，它指向 `base + 250` 的字符串。
3. 手算 `offset_` 应写入的值，再假设文件被重新映射到 `base2`（整体偏移 0x1000），重算 `get()` 返回值。

**预期结果**：`offset_ = 150`；重映射后字段位于 `base2 + 100`，`get()` 返回 `base2 + 100 + 150 = base2 + 250`，仍指向那串字符。偏移量与映射基址无关，这就是 `OffsetPtr` 可序列化的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OffsetPtr` 不能用 `0` 偏移表示「指向自己紧邻的下一个字节」？

> **答**：因为 `offset_ == 0` 被保留为「空指针」语义（`get()` 见到 0 直接返回 `NULL`，`operator bool` 也据此判空）。要表示「紧邻字节」得写偏移 `1`（或更大的对齐值）。这是用「一个非法值换一套可序列化空指针」的常见取舍。

**练习 2**：`Array<T>` 与 `List<T>` 都有 `size` 字段，什么场景下该用 `List`？

> **答**：当数据体量在「写入时才确定」、或希望「同一段词条数据被多处引用」时用 `List`——它的数据在别处，`OffsetPtr` 可以让多个 `List` 指向同一段，省去拷贝；`Array` 则是数据紧跟 `size` 内联，适合「定长、一次性写完、独占」的场景（如 `HeadIndex` 这种按 `num_syllables` 定长的数组）。

---

### 4.2 Table 的磁盘数据模型：词条、多级索引与 Metadata

#### 4.2.1 概念说明

Table 要回答的查询是：**给定一串 `SyllableId`，找出全部编码等于这串音节的词条**。最朴素的办法是把所有 `(Code, Entry)` 排序后二分，但音节序列长度不一（1～7 个音节）、且组合极度稀疏（绝大多数音节组合不成词），扁平存储既慢又浪费空间。

librime 的解法是**固定深度的多级数组索引**，形如一棵「头层稠密数组 + 中间层有序数组 + 末层扁平数组」的退化 trie：

```
HeadIndex  (level 0)   按第 1 个音节 syllable_id 直接下标   ——  O(1)
   └─ TrunkIndex (level 1)  按第 2 个音节二分查找           ——  O(log n)
        └─ TrunkIndex (level 2) 按第 3 个音节二分查找       ——  O(log n)
             └─ TailIndex (level 3) 扁平 LongEntry 数组     ——  O(k) 线性扫描
```

深度被常量 [`Code::kIndexCodeMaxLength = 3`](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L27) 钉死。前 3 个音节走树状索引，**第 4 个及以后的音节**不再继续建层，而是连同词条一起塞进叶子上的 `TailIndex` 扁平数组。这是个刻意的时空权衡，理由见 4.2.2 与综合实践。

#### 4.2.2 核心流程

**为什么头层是数组、中间层是二分？**

头层用第 1 个音节的 `syllable_id` 直接做数组下标。因为 `syllable_id` 是 Prism 分配的**稠密小整数**（`0 .. num_syllables-1`），建一个 `num_syllables` 大小的数组、按下标放节点，既 O(1) 又不浪费——每个音节至少能组成单音节词，槽位几乎全满。

但从第 2 个音节起，组合变得**稀疏**：以「ni」开头的词可能跟「hao」「jian」「hao」……只有少数几种延续。这时再为每个 `syllable_id` 预留槽位会大量空置。于是中间层改成「排序数组 + `key` 字段 + 二分查找」，只为**真正出现过的**延续音节分配节点，省空间，代价是查询变成 O(log n)。

**为什么第 4 个音节起落到 TailIndex？**

继续往下建第 4、5 层，节点会越来越稀疏、指针开销（每个 `OffsetPtr`）越来越大，而四音节以上的长词在词典里占比很小。于是在第 3 层的叶子处「收口」：把该叶子下所有长词条平铺成一个 `TailIndex = Array<LongEntry>`，每条 `LongEntry` 自带 `extra_code`（第 4 个音节起的完整序列）。查询时拿到整个 `TailIndex` 后线性扫描、比对 `extra_code` 即可。长词少、每个叶子的 tail 数组也小，线性扫描的代价可接受。

**字符串怎么存？** 词条文本（如「你好」）和音节串（如「ni」）都是字符串，直接内联会重复存储。Table 用了一个 `StringType` 联合体：要么是内联 `String`（旧版 v1），要么是 `StringId`（一个 `uint32_t`，指向一张独立的 marisa trie）。当前版本（格式 `Rime::Table/4.0`）走 `StringId` 路径：所有字符串先喂进 `StringTableBuilder`，构建期 marisa trie 自动去重并压缩，再把每个字符串对应的 `StringId` 写进 `Entry.text` 与 `Syllabary`。读取时用 `StringTable::GetString(id)` 反查。

#### 4.2.3 源码精读

先看数据结构定义。`Entry` 是「一条词」的最小单元，`LongEntry` 是「带超长编码的词」：

[Entry 与 LongEntry src/rime/dict/table.h:L50-L58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L50-L58) —— `Entry` 只有文本（`StringType`，实际存 `StringId`）和权重（`Weight = float`）；`LongEntry` 多一个 `extra_code`，存放「第 4 个音节起」的剩余序列，正是 TailIndex 用的结构。

`StringType` 用一个宏模拟「带访问器的联合体」，同时兼容内联字符串与字符串 id 两种布局：

[RIME_TABLE_UNION 宏与 StringType src/rime/dict/table.h:L17-L42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L17-L42) —— 联合体的两种形态共享同一片 `int32_t` 内存：当存 `StringId` 时按无符号整数解读，当存内联 `String` 时按 `OffsetPtr<char>` 解读。`str()`/`str_id()` 两个访问器用 `reinterpret_cast` 在两种视图间切换。

三级索引节点一字排开，对比它们的字段差异是理解查询逻辑的钥匙：

[HeadIndexNode / TrunkIndexNode / TailIndex src/rime/dict/table.h:L62-L77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L62-L77) —— 注意三个关键差异：
- `HeadIndexNode` **没有 `key` 字段**，因为它用数组下标当 key（下标 = `syllable_id`）；
- `TrunkIndexNode` **有 `SyllableId key`**，因为它是稀疏排序数组，必须显式存 key 才能二分；
- 三者都有 `entries`（落在该节点的词条）和 `next_level`（指向下一层，`OffsetPtr<PhraseIndex>`）。

`PhraseIndex` 是「中间层 / 末层」的联合体，`Index` 就是头层的别名：

[PhraseIndex 联合体与 Index 别名 src/rime/dict/table.h:L79-L85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L79-L85) —— `PhraseIndex` 同样用 `RIME_TABLE_UNION` 把 `TrunkIndex` 与 `TailIndex` 叠在同一片内存，`next_level->trunk()` 或 `next_level->tail()` 决定按哪种视图读。这样 `OffsetPtr<PhraseIndex>` 一个指针类型就能统一描述「下一层可能是 trunk，也可能是 tail」。

最后是整张表的「目录页」`Metadata`：

[Metadata：整表的文件头 src/rime/dict/table.h:L87-L100](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L87-L100) —— 它位于文件起始偏移 0，记录格式串（`Rime::Table/4.0`）、词典文件校验和、音节数/词条数，以及三个 `OffsetPtr`：`syllabary`（音节名表，按下标存每个音节的字符串）、`index`（头层索引根）、`string_table`（独立的 marisa trie 镜像）。

字符串去重的读写两端：

[GetString / AddString：StringId 的存取 src/rime/dict/table.cc:L228-L237](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L228-L237) —— `AddString` 把字符串交给 `string_table_builder_`（marisa），把返回的 `StringId` 写进 `dest->str_id()`；`GetString` 反向用 `string_table_->GetString(id)` 取回原文。词条文本、音节名全都走这条路，相同字符串只存一份。

`OnBuildFinish` 把 marisa trie 的二进制镜像落盘，`OnLoad` 在运行期从映射区重建 `StringTable`：

[OnBuildFinish / OnLoad：字符串表镜像的构建与挂载 src/rime/dict/table.cc:L244-L263](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L244-L263) —— 构建末尾 `Allocate<char>(image_size)` 在映射区预留一块，`Dump` 把 marisa 镜像拷进去，`Metadata::string_table` 指向它；加载期直接用这块内存构造 `StringTable`，零拷贝。

#### 4.2.4 代码实践

**目标**：在源码层面确认「头层稠密、中间层稀疏」的结构差异。

**操作步骤**：

1. 打开 [table.h:L62-L75](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L62-L75)。
2. 对比 `HeadIndexNode` 与 `TrunkIndexNode` 的字段列表，记录哪个有 `key`、哪个没有。
3. 打开 [table.cc:L157](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L157) 与 [table.cc:L164](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L164)，看 `Walk` 在 level 0 与 level 1 分别如何取节点。

**预期结果**：level 0 直接 `lv1_index_->at[syllable_id]`（数组下标，无需 `key`）；level 1 调 `find_node(...)` 在 `[begin, end)` 区间二分查找 `key == syllable_id` 的节点。结构定义与查询方式一一对应：稠密→下标，稀疏→二分。

#### 4.2.5 小练习与答案

**练习 1**：`HeadIndex = Array<HeadIndexNode>` 的数组长度由什么决定？为什么这个长度几乎不留空槽？

> **答**：长度 = `num_syllables`（见 [BuildHeadIndex 的 CreateArray 调用](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L400)）。因为头层按下标 = `syllable_id` 存放，而 `syllable_id` 是 Prism 分配的稠密整数（0 到 num_syllables-1 连续），且每个音节至少能构成单音节词（如「a」「o」），所以槽位几乎都有节点，浪费极小。

**练习 2**：`PhraseIndex` 为什么要把 `TrunkIndex` 和 `TailIndex` 做成联合体，而不是分两个字段？

> **答**：因为一个 `next_level` 指针所指的「下一层」**要么是 trunk（还能继续二分下钻），要么是 tail（已到末层、扁平数组）**，二者互斥。联合体让同一段内存按需用 `trunk()` 或 `tail()` 视图解读，省去一个 discriminator 字段；判断走哪条路由调用点（`Walk` 里 level 是否到 2）决定，不依赖运行期 tag。

---

### 4.3 TableQuery 与 TableAccessor：逐级遍历与统一读取

#### 4.3.1 概念说明

多级索引建好后，查询就是「沿树根逐层下钻」。但 librime 的查询不是递归函数，而是一个**有状态游标 `TableQuery`**：它记着当前停在第几层、已经走过的音节序列、累计的可信度与全码匹配长度，对外暴露四个动作——

- `Access(syllable_id)`：在**当前层**取出该音节对应的词条，**不改变游标位置**（只读窥探）；
- `Advance(syllable_id)`：**下钻一层**（把音节压入已走路径，游标层 +1）；
- `Backdate()`：**回退一层**（探索另一条分支前的还原动作）；
- `Reset()`：回到根。

为什么设计成可回退的有状态对象？因为 `Table::Query` 要沿 `SyllableGraph` 做 **BFS 广度优先**搜索（一个位置可能延伸出多条音节边），用「`Advance` → 推进 → `Backdate`」的组合，就能在共享同一份游标状态的前提下探索所有分支，比每次重建游标省事。

`TableAccessor` 则是查询结果的**统一读取器**：无论词条来自 `List<Entry>`（普通节点）、`Array<Entry>` 还是 `TailIndex`（长词条），都被包成同一个 `TableAccessor`，对外提供 `Next()`/`entry()`/`code()` 等统一接口，调用方不必关心底层是哪种容器。

#### 4.3.2 核心流程

`TableQuery` 内部维护四个层指针 `lv1_index_`（Head）、`lv2_index_`/`lv3_index_`（Trunk）、`lv4_index_`（Tail），以及 `level_` 标记当前在哪一层。下钻由私有函数 `Walk` 完成，按 `level_` 分四种情况：

```
Walk(syllable_id):               // 下钻前的「定位下一层指针」
  level 0:  lv2 = lv1[syllable_id].next_level.trunk()    // 头层：数组下标
  level 1:  lv3 = lv2.find_node(syllable_id).next_level.trunk()  // 二分
  level 2:  lv4 = lv3.find_node(syllable_id).next_level.tail()   // 二分后转 tail
  else:     false                                       // 已到最底，无法再下钻
```

注意 level 2 的 `Walk` 取的是 `next_level.tail()`——这正是「第 4 层即 TailIndex」的体现。`Access` 的分流与之呼应：level 0 取头层节点 entries，level 1/2 取 trunk 节点 entries，**level 3 直接返回整个 `lv4_index_`（TailIndex）**，调用方拿到后再逐条读 `LongEntry`。

可信度（`credibility`）与全码匹配长度（`quality_len`）是查询时要沿路径**累加**的两个评分。每 `Advance` 一次，就把当前音节的增量压栈（`push_back(sum + delta)`）；`Backdate` 时 `pop_back`。这样任意时刻 `credibility_sum()` 都是从根到当前层的累计可信度，供上层（Dictionary）排序候选用。

`find_node` 是中间层的二分查找，借助 `std::lower_bound` 在按 `key` 升序的 `TrunkIndexNode` 数组里定位。

#### 4.3.3 源码精读

先看游标的下钻与回退，注意四个栈的同步维护：

[Advance / Backdate / Reset：游标状态机 src/rime/dict/table.cc:L102-L136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L102-L136) —— `Advance` 先 `Walk` 定位下一层指针，成功后 `++level_`，并把音节、累计可信度、累计 quality_len、last_pos 四样**同步压栈**；`Backdate` 反向 `--level_` 并 `pop_back` 四样。四个 vector 始终等长，共同描述「从根到当前的路径」。

`Walk` 的四分支是理解整个索引的钥匙：

[Walk：按当前层定位下一层指针 src/rime/dict/table.cc:L152-L183](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L152-L183) —— level 0 用 `lv1_index_->at[syllable_id]` 直接下标（越界或 `next_level` 为空即失败）；level 1/2 用 `find_node` 二分；level 2 成功后把 `lv4_index_` 指向 `next_level.tail()`（切到 TailIndex 视图）；超过 level 2 返回 false（已到底）。

中间层的二分查找：

[node_less 与 find_node：TrunkIndex 的二分查找 src/rime/dict/table.cc:L138-L150](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L138-L150) —— 构造一个临时 `target` 节点只填 `key`，用 `std::lower_bound` 找首个不小于 key 的位置，再判等。这要求 `TrunkIndex` 在构建时已按 `key` 升序排好（见 4.4 的 `BuildTrunkIndex`，它按 `vocabulary` 即 `map<int,...>` 的升序遍历写入，天然有序）。

`Access` 的分流——注意 level 3 如何一把返回整个 tail：

[Access：在当前层取词条（不移动游标） src/rime/dict/table.cc:L190-L217](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L190-L217) —— level 0 返回头层节点的 `entries`；level 1/2 返回 trunk 节点的 `entries`；level 3 **无视传入的 `syllable_id`**，直接把整个 `lv4_index_`（TailIndex）包进 `TableAccessor`——因为 tail 已经是末层扁平数组，没有「按第 4 音节再索引」一说，整包返回由调用方逐条比对 `extra_code`。

`TableAccessor` 把三种容器统一成一种读取视图：

[TableAccessor 的三种构造与读取 src/rime/dict/table.cc:L24-L100](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L24-L100) —— 三个构造重载分别接受 `List<Entry>*`、`Array<Entry>*`、`TailIndex*`，前两者填 `entries_`，后者填 `long_entries_`。`entry()` 对长词条取 `&long_entries_[cursor_].entry`；`extra_code()` 只对长词条返回 `&long_entries_[cursor_].extra_code`；`code()` 则把「已走路径 index_code_」与「extra_code」拼成完整编码。于是调用方用同一套 `while(!a.exhausted()){ a.entry(); a.Next(); }` 就能遍历任意来源的词条。

#### 4.3.4 代码实践

**目标**：追踪一个 4 音节词条（如「中华人民共和国」）被 `TableQuery` 取回的过程，看清 TailIndex 的「整包返回」。

**操作步骤**：

1. 设该词条编码为 `[s1, s2, s3, s4, s5, s6, s7]`（7 个音节）。
2. 假设游标已 `Advance(s1)`、`Advance(s2)`、`Advance(s3)`，此时 `level_ == 3`、`lv4_index_` 指向 `s1.s2.s3` 叶子的 TailIndex。
3. 阅读 [table.cc:L211-L214](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L211-L214)，确认 level 3 的 `Access` 把整个 `lv4_index_` 当 `TailIndex` 包进 accessor。
4. 再读 [table.cc:L68-L93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L68-L93)，看 `entry()`/`extra_code()`/`code()` 如何还原出 `[s1,s2,s3,s4,s5,s6,s7]`。

**预期结果**：游标停在 level 3，`Access` 返回的 accessor 内部 `long_entries_` 指向整个 tail 数组；遍历时每条 `LongEntry` 的 `extra_code = [s4,s5,s6,s7]`，`code()` 返回 `index_code_([s1,s2,s3]) + extra_code = [s1..s7]`，与原始编码一致。第 4 个及以后的音节不在索引里，靠 `extra_code` 字段逐条携带。

#### 4.3.5 小练习与答案

**练习 1**：`Access` 与 `Advance` 都接收 `syllable_id`，它们最本质的区别是什么？

> **答**：`Access` 是**只读窥探**——在当前层取该音节的词条，不改变 `level_` 与任何栈，可重复调用；`Advance` 是**移动游标**——先 `Walk` 把下一层指针就位，再 `++level_` 并压栈，调用后游标深度 +1，通常配合 `Backdate` 使用以探索兄弟分支。

**练习 2**：为什么 `Access` 在 level 3 时「无视」传入的 `syllable_id`，直接返回整个 TailIndex？

> **答**：因为 TailIndex 是叶子层的扁平 `Array<LongEntry>`，**没有为「第 4 个音节」再建索引**（这正是 4.2 讲的「收口」设计）。整包返回后，由 `TableAccessor` 暴露每条的 `extra_code`，让上层（Dictionary）自行比对剩余音节。换句话说，前 3 个音节有结构化索引，第 4 个起退化为线性扫描。

---

### 4.4 构建链路 Build 与查询入口 Query

#### 4.4.1 概念说明

前面两节讲清了「数据长什么样」和「游标怎么走」，本节把它们串起来：构建期 `Build*Index` 把内存态的 `Vocabulary` 树「灌」进映射区，生成多级索引；运行期 `Table::Query` 沿 `SyllableGraph` 做 BFS，用 `TableQuery` 游标把每条路径的候选收集进按结束位置分组的 `TableQueryResult`。

构建期的输入 `Vocabulary` 是一棵 `map<int, VocabularyPage>` 的内存树（[vocabulary.h:L95-L104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L95-L104)）。它有个关键约定：`Vocabulary::LocateEntries` 在插入词条时，**前 `kIndexCodeMaxLength` 层用真实 `syllable_id` 做 key，到了第 4 层一律用 `-1` 做 key**（见 [vocabulary.cc:L123-L141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.cc#L123-L141) 的 `if (i == n-1 || i == kIndexCodeMaxLength)` 与 `key = (i < kIndexCodeMaxLength) ? code[i] : -1`）。于是 `BuildTailIndex` 只需 `vocabulary.find(-1)` 就能拿到所有「超长编码」的词条页——这是构建与查询两端对「第 4 层收口」这一约定的镜像。

#### 4.4.2 核心流程

**构建（自顶向下递归）**：

```
Build(syllabary, vocabulary)
  ├─ Create(预估容量) + Allocate<Metadata>
  ├─ 建音节表 syllabary_ = CreateArray<StringType>(num_syllables)
  ├─ index_ = BuildHeadIndex(vocabulary, num_syllables)
  │     对每个 syllable_id：
  │       BuildEntryList(entries -> node.entries)
  │       若有 next_level：BuildTrunkIndex([sid], *next_level)
  ├─ BuildTrunkIndex(prefix, vocabulary)         // 中间层，可递归
  │     对每个 (key=sid, page)：
  │       node.key = sid；BuildEntryList(...)
  │       若 code.size() < kIndexCodeMaxLength：递归 BuildTrunkIndex
  │       否则：BuildTailIndex(code, *next_level)  ← 收口！
  ├─ BuildTailIndex(prefix, vocabulary)
  │     page = vocabulary.find(-1)->second         ← 取「-1 页」
  │     对每条词：extra_code = code[kIndexCodeMaxLength .. end]
  └─ OnBuildFinish()：构建 marisa 字符串表镜像、写 format 串
```

**查询（沿 SyllableGraph 的 BFS）**：

```
Query(syll_graph, start_pos, result)
  queue ← { (start_pos, TableQuery(index_)) }
  while queue 非空：
    (pos, query) ← queue.pop()
    若 pos 在 syll_graph.indices 中无音节：continue
    若 query.level() == kIndexCodeMaxLength：        // 已下钻 3 层
      accessor = query.Access(-1)                    // 取整包 tail
      result[pos].push_back(accessor)；continue
    对 pos 处每个 (syll_id, spellings)：
      对每条 spelling（含 end_pos、credibility）：
        accessor = query.Access(syll_id, 累计cred, 全码长度增量)
        若非空：result[end_pos].push_back(accessor)
        若 end_pos 未到尽头 且 Advance(syll_id) 成功：
          queue.push({end_pos, query})；query.Backdate()   ← 还原以探索兄弟边
```

关键点：`result` 是 `map<int, vector<TableAccessor>>`，**以结束位置 `end_pos` 为键**分组。这样上层（Dictionary）拿到的是「输入串从 `start_pos` 起、到各个 `end_pos` 为止的所有候选」，便于按词长和可信度挑选。BFS 用「`Advance` 后立刻 `Backdate`」的套路，保证同一个 `query` 状态能反复探索同一个位置延伸出的多条音节边。

#### 4.4.3 源码精读

`Build` 是构建总入口，留意它对文件大小的预估与各部件的装配顺序：

[Build：构建总入口 src/rime/dict/table.cc:L330-L390](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L330-L390) —— 预估容量 \(\text{size} \approx 4096 + 32 \cdot \text{num\_syllables} + 64 \cdot \text{num\_entries}\)（行 336-337），先 `Create` 预留、不够时 `Allocate` 会倍增；随后依次建 Metadata、音节表、`BuildIndex`，最后 `OnBuildFinish` 落字符串表、写 `format` 串「Rime::Table/4.0」。

头层与中间层的递归构建，注意 `BuildTrunkIndex` 里的「收口」判断：

[BuildHeadIndex src/rime/dict/table.cc:L398-L422](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L398-L422) —— 按 `vocabulary`（`map<int,...>`）遍历，`index->at[syllable_id]` 直接按下标放节点（验证了头层的稠密数组布局），有 `next_level` 就递归 `BuildTrunkIndex`。

[BuildTrunkIndex：中间层与收口判断 src/rime/dict/table.cc:L424-L459](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L424-L459) —— 行 442 是核心：`if (code.size() < Code::kIndexCodeMaxLength)` 继续递归 trunk，`else` 转 `BuildTailIndex`。因为 `vocabulary` 是 `map` 按升序遍历，写入的 `TrunkIndexNode` 天然按 `key` 升序，满足 `find_node` 二分前提。

[BuildTailIndex：取「-1 页」并切出 extra_code src/rime/dict/table.cc:L461-L490](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L461-L490) —— 行 463 `vocabulary.find(-1)` 正是拿「第 4 层及以后」的词条页；行 477 `extra_code_length = code.size() - kIndexCodeMaxLength`，行 485 用 `std::copy` 把第 4 个音节起的序列拷进 `LongEntry.extra_code`。这就把「超过 3 个音节」的词条统一落进了 TailIndex。

运行期查询的 BFS：

[Table::Query：沿 SyllableGraph 的广度优先搜索 src/rime/dict/table.cc:L571-L630](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L571-L630) —— 队列元素是 `(位置, TableQuery)`。行 588 判断「已下钻到 kIndexCodeMaxLength 层」时用 `Access(-1)` 取整包 tail；行 615-616 对每条音节边 `Access` 收集候选到 `result[end_pos]`；行 620-624 若未到尽头就 `Advance` 后把新状态入队、再 `Backdate` 还原游标以探索兄弟边。`result` 按 `end_pos` 分组，正是上层 Dictionary 按词长挑选候选所需的形态。

两个便捷查询入口，对比它们对游标的使用差异：

[QueryWords / QueryPhrases src/rime/dict/table.cc:L550-L566](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L550-L566) —— `QueryWords(sid)` 是单音节查询，建个新游标直接 `Access(sid)`；`QueryPhrases(code)` 按完整编码查：前 `kIndexCodeMaxLength` 个音节逐个 `Advance`，若 code 更长则最后 `Access(-1)` 取 tail。后者再次印证了「前 3 个音节走索引、之后走 tail」的约定。

#### 4.4.4 代码实践

**目标**：追踪一条「ni hao」（2 音节）词条和一条 4 音节词条在 `QueryPhrases` 下的不同归宿，亲手验证 TailIndex 的触发条件。这是本讲核心实践任务的源码跟踪版。

**操作步骤**：

1. 设 `ni=sid_ni`、`hao=sid_hao`，词条「你好」编码 `[sid_ni, sid_hao]`（长度 2）。
2. 在 [table.cc:L555-L566](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L555-L566) 里代入 `code.size()==2`：循环 `i=0` 时 `code.size()==i+1`（2==1? 否），`Advance(code[0]=sid_ni)` 成功；`i=1` 时 `code.size()==i+1`（2==2? 是），`return query.Access(code[1]=sid_hao)`。结果来自 trunk 节点的 `entries`。
3. 再设一条 4 音节词条编码 `[a,b,c,d]`：循环 `i=0,1,2` 三次 `Advance`（均非 `size==i+1`），循环结束后 `return query.Access(-1)`，命中 [table.cc:L211-L214](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L211-L214) 的 level-3 分支，返回整包 TailIndex。
4. 对照 [BuildTrunkIndex 的收口判断](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L442-L455)，确认「构建时 code.size()≥3 走 tail」与「查询时 4 音节走 tail」是同一套 `kIndexCodeMaxLength` 约定的两端。

**预期结果**：2 音节词条走 trunk entries（O(log n) 二分定位），4 音节词条走 TailIndex 整包返回（之后线性扫描 extra_code）。两端都由 `Code::kIndexCodeMaxLength == 3` 这一个常量统辖。

**关于运行**：本实践为源码阅读型，无需编译运行。若想实地观察，可在本地 `make` 构建 librime 后，用 `rime_api_console` 配合一个含 4 音节词的方案输入长词，但具体输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `BuildTailIndex` 用 `vocabulary.find(-1)` 而不是 `find(syllable_id)` 来取词条？

> **答**：因为构建期 `Vocabulary::LocateEntries` 在塞入「第 4 层及以后」的词条时，统一用 `-1` 做 key（[vocabulary.cc:L128-L132](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.cc#L128-L132)），真实音节被存进 `ShortDictEntry::code` 里。所以「-1 页」就是「所有超长编码词条」的收纳处，`find(-1)` 一把取全。`-1` 是合法 `syllable_id`（非负 int）之外的哨兵值。

**练习 2**：`Table::Query` 的 BFS 里，`query.Advance(...)` 之后紧跟 `query.Backdate()`，为什么这样能正确探索同一位置的多条音节边？

> **答**：`Advance` 把游标下钻一层并压栈，随后把「已下钻的新状态」**拷贝**一份入队（`q.push({end_pos, query})`，入队的是值拷贝）；接着对**当前**游标调用 `Backdate` 还原到下钻前的状态，于是同一个 `query` 可以继续处理当前位置的**下一条**音节边。每条边都得到一个独立的下游状态副本，互不干扰。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个「**解释为什么超过 `kIndexCodeMaxLength` 个音节的词条要落到 TailIndex，并说明这对长词查询的影响**」的完整论证任务（这正是本讲规格指定的实践任务）。

**任务**：请撰写一段 200～400 字的说明，回答以下三个子问题，并在源码中找到证据支撑每一条：

1. **触发条件**：词条编码长度等于几时会被收口进 TailIndex？依据是哪一行构建代码、哪一个常量？
2. **设计动机**：为什么不继续为第 4、5 个音节建 TrunkIndex 层，而选择扁平化？从「稀疏度」与「指针开销」两个角度分析。
3. **查询影响**：长词查询走 TailIndex 时，返回的是什么？上层（Dictionary/`TableAccessor`）如何从中还原出完整编码？相比短词的 O(log n) 二分，长词的查询复杂度变成了什么？

**参考作答要点**（写完后对照）：

1. 当 `code.size() > kIndexCodeMaxLength`（即 `> 3`，[vocabulary.h:L27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L27)）时收口。证据：[table.cc:L442-L455](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L442-L455) 的 `if (code.size() < Code::kIndexCodeMaxLength)` 分支。
2. 动机：① 音节组合随深度迅速稀疏化（「ni hao」常见，「ni hao de na」极少），继续建层会留下大量空节点；② 每个中间节点要带 `OffsetPtr<PhraseIndex>` 指针与 `List<Entry>` 头部，深层稀疏节点的指针开销会超过其承载的词条数据。扁平成 `Array<LongEntry>` 后，每个叶子只存一份紧凑数组，空间更省。
3. 影响：`Access(-1)` 返回整个 `TailIndex`（[table.cc:L211-L214](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L211-L214)），`TableAccessor` 用 `long_entries_` 视图遍历，靠 `extra_code()` 取第 4 个音节起的序列、靠 `code()` 把 `index_code_` 与 `extra_code` 拼成完整编码（[table.cc:L77-L93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L77-L93)）。复杂度从短词的 O(log n) 退化为对该叶子 tail 数组的 **O(k) 线性扫描**，且需逐条比对 `extra_code`；但因长词少、单叶子 tail 小，实际代价可接受。

## 6. 本讲小结

- `Table` 是一张磁盘上的**固定深度多级索引**：头层（HeadIndex）按 `syllable_id` 稠密下标 O(1)，中间层（TrunkIndex）有序数组 + 二分 O(log n)，末层（TailIndex）扁平数组。
- 深度被 `Code::kIndexCodeMaxLength == 3` 钉死：前 3 个音节走结构化索引，**第 4 个音节起**连同词条一起收口进 `TailIndex`，用 `LongEntry.extra_code` 携带剩余音节。
- `TableQuery` 是一个**有状态游标**，靠 `Walk`（头层下标 / 中间层 `find_node` 二分）定位下一层，靠 `Advance`/`Backdate`/`Reset` 维护四栈（音节、累计可信度、累计 quality_len、last_pos）。
- `TableAccessor` 把 `List<Entry>` / `Array<Entry>` / `TailIndex` 三种来源**统一**成 `Next()`/`entry()`/`code()` 接口，对长词条用 `extra_code()` 还原完整编码。
- 序列化由 `MappedFile` 基座支撑：`OffsetPtr` 自相对寻址使指针可序列化、`Array`/`List` 提供定长容器、`Allocate` 按 `alignof` 对齐并倍增扩容。
- 字符串（词条文本、音节名）经 `StringType`（`StringId` 联合体）存进独立的 **marisa trie**（`StringTable`）实现去重压缩；`Syllabary` 是按 `syllable_id` 下标的音节名数组。
- 查询入口 `Table::Query` 沿 `SyllableGraph` 做 **BFS**，用「`Access` 收候选 → `Advance` 入队 → `Backdate` 还原」探索所有切分路径，结果按结束位置 `end_pos` 分组。

## 7. 下一步学习建议

本讲把「`syllable_id` 序列 → 词条集合」的查询讲到了底。接下来：

- **u8-l4（DictCompiler 构建流程）**：看 `EntryCollector` 如何解析 `*.dict.yaml`、`Vocabulary` 树如何被 `LocateEntries` 填成「前 3 层真实音节 + 第 4 层 -1 哨兵」的形态——本讲构建侧的输入正是它产出的。
- **u8-l5（Dictionary 查询主链路）**：看 `Dictionary::Lookup` 如何把 `SyllableGraph` 喂给本讲的 `Table::Query`，并把 `TableQueryResult`（按 `end_pos` 分组的 accessor）翻译成 `DictEntryCollector` 供翻译器消费；`Decode` 如何把 `syllable_id` 还原成可读拼写。
- 若对序列化地基感兴趣，可延伸阅读 `src/rime/dict/string_table.cc`（marisa trie 的封装）与 `src/rime/dict/mapped_file.cc`（平台相关的 mmap 实现），理解 `OffsetPtr` 之外的真实文件 I/O。
