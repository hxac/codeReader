# pack 文件格式与打包存储

## 1. 本讲目标

上一讲（u3-l2）我们看清了「单个对象如何以松散对象的形式落盘」：一个对象一个文件，内容做 zlib 压缩。这种格式简单直观，但有一个致命弱点——**对象一多，文件数就爆炸**。一个有几十万个对象的中型仓库，光 `.git/objects/` 下就会挤满几十万个文件，inode 耗尽、磁盘寻道开销巨大、克隆时逐个传输慢得无法忍受。

git 的解法是 **pack（打包）**：把大量对象拼接进**一个** `.pack` 文件，再用一个 `.idx` 索引文件提供按哈希快速定位的能力，并在拼接时用 **delta 压缩**进一步消除对象间的冗余。

学完本讲，你应当能够：

- 说清楚一个 `.pack` 文件在磁盘上的字节布局：文件头、对象记录、校验尾部。
- 说清楚一个 `.idx`（v2）索引文件的结构，以及 git 如何用它**两步**定位任意对象。
- 理解两种 delta 编码（`OFS_DELTA` 与 `REF_DELTA`）的区别，以及读取 delta 对象时的「向下钻孔 + 向上还原」过程。
- 看懂 `pack-write.c`（写入）与 `packfile.c`（读取）里相关函数的真实代码。

## 2. 前置知识

本讲默认你已经掌握：

- **内容寻址**：git 用对象内容的哈希（SHA-1 为 40 字符、SHA-256 为 64 字符）作为对象的名字（u3-l1、u3-l2）。
- **四种对象类型**：blob / tree / commit / tag，外加 pack 内部专用的两种 delta 类型（u3-l1）。
- **松散对象格式**：`"类型 长度\0" + 内容` 整段做 zlib 压缩（u3-l2）。

几个本讲会用到的术语，先建立直觉：

- **pack**：一个把多个对象首尾拼接的二进制文件，后缀 `.pack`。
- **idx（索引）**：与 pack 配对的索引文件，后缀 `.idx`，记录「对象哈希 → 在 pack 中的字节偏移」。
- **fanout（扇出）表**：一张 256 项的「前缀计数」表，用来把二分查找的范围迅速缩小到同一首字节的桶里。
- **delta（增量）**：不存对象完整内容，只存「相对某个基础对象（base）的差异」，读取时用 base 打补丁还原。
- **offset（偏移）**：某个对象在 pack 文件中从文件开头算起的字节位置。

一句话直觉：**pack 是一本厚厚的「对象合订本」，idx 是这本合订本的「音序目录」，delta 是合订本里「参见第 X 页、再改这几处」的省纸写法。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pack.h` | pack 与 idx 的磁盘结构定义（`struct pack_header`、`struct pack_idx_entry`、各种签名与版本宏）。 |
| `pack-write.c` | **写入侧**：写 pack 头、写 idx 文件、把临时 pack/idx 原子改名落地。 |
| `packfile.c` | **读取侧**：mmap idx、按 fanout 二分查找、按偏移读对象、解析 delta。 |
| `csum-file.c` | 提供 `hashfile` 抽象：边写边算哈希、在文件末尾追加校验尾部。 |
| `delta.h` | delta 的生成（`diff_delta`）与还原（`patch_delta`）接口。 |

记住一个分工口诀：**`pack-write.c` 造合订本，`packfile.c` 翻合订本，`csum-file.c` 给合订本盖骑缝章，`delta.h` 描述省纸写法的规则。**

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **pack 文件头与对象记录**——一个 `.pack` 文件内部长什么样。
2. **.idx 索引与查找**——git 如何在几百万对象里按哈希秒级定位。
3. **delta 压缩 base/offset**——两种 delta 编码与还原过程。

### 4.1 pack 文件头与对象记录

#### 4.1.1 概念说明

一个 `.pack` 文件在磁盘上是一段连续字节，自上而下三段：

```
┌─────────────────────────────┐
│  pack header（12 字节）      │  ← "PACK" 签名 + 版本 + 对象总数
├─────────────────────────────┤
│  对象记录 0                  │
│  对象记录 1                  │  ← 每条记录：变长头 + zlib 压缩数据
│  对象记录 2                  │
│  ...                         │
├─────────────────────────────┤
│  校验尾部（哈希，20 或 32 字节）│  ← 对前面所有字节的哈希
└─────────────────────────────┘
```

注意 pack 文件**自身没有对象哈希表**——它只是按写入顺序把对象排成一列。要按哈希找对象，必须借助配套的 `.idx`（见 4.2）。pack 文件里每个对象的「身份」由它在文件中的**字节偏移**（offset）来表示。

#### 4.1.2 核心流程

写一个 pack 文件的流程：

1. 用 `create_tmp_packfile` 开一个临时文件，拿到一个 `hashfile` 写句柄（边写边算哈希）。
2. `write_pack_header` 写入 12 字节文件头。
3. 逐个对象写入：先 `encode_in_pack_object_header` 编码变长头，再写 zlib 压缩后的内容（或 delta 数据）。
4. 全部写完后，`finalize_hashfile` 把累计的哈希作为校验尾部追加到文件末尾。
5. `stage_tmp_packfiles` 生成配套 `.idx`，并把 `.pack`/`.idx` 从临时名原子改名为最终的 `pack-<哈希>.pack` / `pack-<哈希>.idx`。

读取时，`open_packed_git_1` 会做严格校验：读文件头确认签名与版本、确认对象数与 idx 一致、比对 pack 尾部哈希与 idx 里记录的 pack 哈希是否相等。

#### 4.1.3 源码精读

**文件头结构**定义在 `pack.h`：

[pack.h:14-22](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack.h#L14-L22) — 定义 pack 签名 `"PACK"`（`0x5041434b`）、版本号 `2`，以及 `struct pack_header`（签名 + 版本 + 对象数，各 4 字节，共 12 字节）。

写入文件头的函数极其直白：

[pack-write.c:364-373](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack-write.c#L364-L373) — `write_pack_header` 把签名、版本、对象数三个字段用 `htonl` 转成网络字节序（大端）后写出。注意对象数在写头时可能还不知道（流式打包时对象是一个个到来的），所以有 `fixup_pack_header_footer` 在事后回填真实的对象数并重算尾部校验。

**每个对象记录的变长头**编码规则在注释里说得很清楚：

[pack-write.c:500-528](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack-write.c#L500-L528) — `encode_in_pack_object_header`：
- 第一字节：低 4 位是 size 的低位，中间 3 位是对象类型，最高位是「size 是否还有后续」。
- 后续字节：每字节贡献 7 位 size，最高位同样是「是否继续」。

这种「类型 3 位 + 变长 size」的编码，让小对象的头只需 1 字节，大对象最多也只要 10 字节（`MAX_PACK_OBJECT_HEADER`）。

读取侧对应的解析函数是 `unpack_object_header`：

[packfile.c:957-981](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L957-L981) — 通过 `use_pack` 把 pack 文件对应窗口映射进内存，再用 `unpack_object_header_buffer` 解出类型与 size，并把读指针 `curpos` 前移。

**校验尾部**由 `csum-file.c` 的 `hashfile` 抽象负责。每调用一次 `hashwrite`，数据既落盘又被喂进哈希上下文：

[csum-file.c:113-145](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/csum-file.c#L113-L145) — `hashwrite` 在把数据写出的同时，用 `git_hash_update` 累计哈希、可选地累计 CRC32（idx v2 要用）。

[csum-file.c:65-102](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/csum-file.c#L65-L102) — `finalize_hashfile` 在收尾时算出最终哈希，当带 `CSUM_HASH_IN_STREAM` 标志时把这段哈希**追加进文件流**，这就是 pack/idx 末尾的校验尾部。

读取时，`open_packed_git_1` 会交叉校验 pack 与 idx 是否匹配：

[packfile.c:529-596](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L529-L596) — 依次校验：签名是 `"PACK"`、版本被支持、pack 头里的对象数 == idx 记录的对象数、pack 文件末尾自带的哈希 == idx 里记录的 pack 哈希。任一项不符就拒绝打开这个 pack。

#### 4.1.4 代码实践

**实践目标**：亲手制造一个 pack，并用十六进制工具确认它的文件头确实是 `PACK`。

**操作步骤**：

```bash
# 1. 建一个临时仓库并制造若干对象
mkdir /tmp/pack-demo && cd /tmp/pack-demo
git init -q
for i in 1 2 3 4 5; do echo "line $i" > f$i.txt; git add f$i.txt; done
git commit -q -m "demo"

# 2. 触发打包（默认就会把松散对象合并进 pack）
git gc

# 3. 找到生成的 pack 文件
ls -la .git/objects/pack/

# 4. 看文件头前 12 字节（应当以 50 41 43 4b 即 "PACK" 开头）
xxd -l 12 .git/objects/pack/*.pack | head
```

**需要观察的现象**：

- `git gc` 之后，`.git/objects/pack/` 下应出现两个文件：`pack-<40 位哈希>.pack` 与 `pack-<同一哈希>.idx`。
- `xxd` 输出的前 4 字节应为 `5041 4b50`（ASCII 即 `PACK`），随后 4 字节是版本 `0000 0002`，最后 4 字节是对象数（小端写入前会先经 `htonl`，所以磁盘上是 `0000 00xx`）。
- pack 文件最末尾 20 字节（SHA-1）应等于文件名里那串哈希。

**预期结果**：能清楚看到 pack 的「签名 + 版本 + 对象数」三段式文件头，并理解末尾 20 字节是整个文件内容的 SHA-1。若 `xxd` 不可用，可改用 `od -A x -t x1z -v`。本步骤的命令输出格式为「待本地验证」，但 `git gc` 生成 `.pack`/`.idx` 的行为是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：pack 文件头里 `hdr_entries` 记录的是「对象数」，而单个对象的哈希并不在 pack 头里。那么给定一个对象哈希，git 如何知道它在 pack 里的位置？

**答案**：靠配套的 `.idx` 索引。pack 文件本身只按写入顺序排列对象，对象的「身份」是它在 pack 中的字节偏移；`.idx` 才是「哈希 → 偏移」的映射表（详见 4.2）。

**练习 2**：`encode_in_pack_object_header` 里第一字节的「中间 3 位是类型」。已知 `OBJ_COMMIT=1`、`OBJ_TREE=2`、`OBJ_BLOB=3`、`OBJ_OFS_DELTA=6`，那么一个大小为 5 的 commit 对象，第一字节是多少？

**答案**：类型左移 4 位后与 size 低 4 位相或：\((\text{type} \ll 4) \;|\; (\text{size} \,\&\, 15) = (1 \ll 4) \;|\; 5 = 16 + 5 = 21\)，即十六进制 `0x15`。由于 size=5 已被低 4 位（最大 15）装下，无需后续字节。

---

### 4.2 .idx 索引与查找

#### 4.2.1 概念说明

`.idx` 是 pack 的「目录」。给定对象哈希，git 要在可能上百万的对象里找到它，绝不能线性扫描——`.idx` 把这件事做到接近 \(O(\log n)\)。当前使用的是 **idx v2**，它比古老的 v1 多了 CRC32 表与 64 位偏移支持。

idx v2 文件的字节布局：

```
┌───────────────────────────────────┐
│  魔数 \377tOc + 版本 2（8 字节）    │
├───────────────────────────────────┤
│  fanout 表：256 × 4 字节           │  ← 累计计数，定位首字节桶
├───────────────────────────────────┤
│  排好序的对象哈希表：nr × hashsz    │
├───────────────────────────────────┤
│  CRC32 表：nr × 4 字节             │  ← v2 新增，用于校验
├───────────────────────────────────┤
│  32 位偏移表：nr × 4 字节          │  ← 高位置 1 表示走 64 位大偏移表
├───────────────────────────────────┤
│  64 位大偏移表：（可选）若干 × 8 字节│  ← 仅当 pack > 2 GiB 才需要
├───────────────────────────────────┤
│  pack 的哈希（hashsz）             │
├───────────────────────────────────┤
│  idx 自身的哈希（hashsz）          │
└───────────────────────────────────┘
```

**fanout 表**是加速的关键。它有 256 项，第 `i` 项存的是「哈希首字节 ≤ `i` 的对象总数」。由于对象哈希表已排序，给定一个目标哈希 `h`，只要读 `fanout[h[0]-1]` 与 `fanout[h[0]]`，就把二分查找的范围直接限定在「首字节等于 `h[0]`」的那一段，等于一次性省掉了哈希前 8 位的比较。`write_idx_file` 的注释把这说成「avoid having to do eight extra binary search iterations」（省下 8 次二分迭代）。

#### 4.2.2 核心流程

按哈希查对象的「两步走」：

1. **查 idx 得到序号**：`bsearch_pack` 用 fanout 表缩小范围，在哈希表里二分，命中后返回对象在 idx 中的**序号** `result`（0 起）。
2. **由序号拿到 pack 偏移**：`nth_packed_object_offset(p, result)` 读偏移表。若 32 位偏移的最高位为 0，直接是 pack 内字节偏移；若最高位为 1，则低 31 位是「64 位大偏移表」的下标，再去那里读真正的 8 字节偏移。
3. 拿到偏移后交给 `unpack_entry`（4.3）读取对象内容。

读取整个 idx 文件的入口是 `load_idx`：它把整个 `.idx` 一次性 `mmap` 进内存，校验大小、版本、fanout 单调性，然后把 `index_data`、`index_version`、`num_objects` 等填进 `struct packed_git`。

#### 4.2.3 源码精读

**idx 签名与结构定义**：

[pack.h:41-83](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack.h#L41-L83) — `PACK_IDX_SIGNATURE` 是魔数 `0xff744f63`（ASCII `\377tOc`），它的设计很巧妙：这个值大到不可能是一个合法的 v1 fanout 计数（注释解释了 v1 pack 最多约 14 亿对象），所以旧版 git 一看就知道这是新格式。`struct pack_idx_entry` 是写入 idx 时每个对象的「哈希 + CRC32 + 偏移」三元组。

**写入 idx 文件**的主函数，集中体现了上面那张布局图：

[pack-write.c:57-180](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack-write.c#L57-L180) — `write_idx_file` 按顺序写出各段：
- 行 101-106：写 v2 头（魔数 + 版本）。
- 行 113-123：写 256 项 fanout 表，每项是「到目前为止首字节 ≤ i 的对象累计数」。
- 行 128-138：写排好序的对象哈希表。
- 行 144-148：写 CRC32 表。
- 行 151-160：写 32 位偏移表；当某对象偏移 ≥ \(2^{31}\) 时，写入 `0x80000000 | 大偏移表下标`（即用最高位置 1 作「溢出」标记）。
- 行 163-172：写 64 位大偏移表（仅当存在超大 pack）。

**版本选择**也很值得注意：

[pack-write.c:97-98](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack-write.c#L97-L98) — 如果最后一个对象的偏移 ≥ \(2^{31}\)（pack 大于 2 GiB），就**强制升到 v2**，因为只有 v2 有 64 位偏移表。

**读取 idx 并校验**：

[packfile.c:106-184](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L106-L184) — `load_idx` 检查文件最小尺寸、识别版本（有魔数则 v2，否则 v1）、遍历 fanout 表验证**单调不减**（`if (n < nr) error("non-monotonic index")`），并按版本校验文件总大小是否吻合，最后记下 `crc_offset` 供 CRC 校验用。

**按哈希二分查找**的核心：

[packfile.c:1734-1756](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1734-L1756) — `bsearch_pack`：跳过 fanout 表头算出 `index_lookup`（哈希表起点）与每条记录的宽度 `index_lookup_width`，然后委托给通用的 `bsearch_hash`（定义在 `hash-lookup.c`）。`bsearch_hash` 内部正是先用 fanout 表定位首字节桶，再在该桶内二分。

**由序号取偏移**：

[packfile.c:1796-1814](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1796-L1814) — `nth_packed_object_offset`：v2 时读 32 位偏移，若最高位为 0 直接返回；否则用低 31 位作下标，去 64 位大偏移表里 `get_be64` 读出真正偏移。

**封装两步走的便捷函数**：

[packfile.c:1816-1830](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1816-L1830) — `find_pack_entry_one` 把「bsearch 得序号 → 取偏移」串起来，返回对象在该 pack 中的字节偏移；找不到返回 0。

#### 4.2.4 代码实践

**实践目标**：用 `git verify-pack` 直接「打印」一个 idx 的内容，把目录里的每一段对应到源码。

**操作步骤**：

```bash
cd /tmp/pack-demo   # 复用 4.1.4 的仓库

# 1. 列出 pack 里每个对象：哈希、类型、大小、偏移、delta 信息
git verify-pack -v .git/objects/pack/*.idx | head -20
```

**需要观察的现象**：

- `git verify-pack -v` 输出形如：
  ```
  <哈希> blob   <大小> <pack内偏移> <解压后大小> <深度> <delta基的哈希或自身哈希>
  ```
- 注意每行的「pack 内偏移」列——它正是 `nth_packed_object_offset` 返回的那个字节偏移。
- 末尾几行会有「non delta」与「chain length = N」的统计，这是 delta 链深度的汇总。

**预期结果**：你能把 `verify-pack` 输出的每一列，分别对应到 idx 文件里的「哈希表」「偏移表」以及 `load_idx` 校验过的字段。结合 `bsearch_pack` 的代码，复述一次「哈希 → 序号 → 偏移」的完整路径。

#### 4.2.5 小练习与答案

**练习 1**：idx v2 相比 v1 多了「CRC32 表」。这个 CRC32 是校验谁的、有什么用？

**答案**：它校验的是**每个对象在 pack 中的那段原始（压缩后）字节**。读取对象时（见 `unpack_entry` 里 `do_check_packed_object_crc` 分支），git 可以用 idx 里存的 CRC32 快速核对对象的压缩数据有没有在磁盘上损坏，而不必把对象完全解压并重算内容哈希——开销低得多。

**练习 2**：为什么偏移表要用「32 位 + 可选 64 位大偏移表」两段，而不是直接每项都用 8 字节？

**答案**：绝大多数 pack 小于 2 GiB，偏移用 4 字节足够；只有极少数超大 pack 才需要 64 位。两段式让常见情况省一半空间（上百万对象 × 4 字节就是好几 MB），而用「最高位置 1」作为溢出标记，几乎零成本地兼容了超大 pack。

---

### 4.3 delta 压缩 base/offset

#### 4.3.1 概念说明

git 仓库里大量对象彼此高度相似：同一个文件的历史版本、同一棵目录树在不同提交下的细微变化。delta 压缩就是利用这一点——**不存对象完整内容，只存「相对某个基础对象（base）的差异」**。

delta 对象不是「一等对象类型」，而是 pack 内部的两种记录类型：

| 类型 | 值 | base 的标识方式 |
| --- | --- | --- |
| `OBJ_OFS_DELTA` | 6 | 存 base 在**同一 pack 中的相对字节偏移**（往回指）。 |
| `OBJ_REF_DELTA` | 7 | 存 base 的**完整对象哈希**（20 或 32 字节）。 |

二者权衡：

- **OFS_DELTA**：base 用变长偏移表示，通常只需 1~3 字节，**省空间**，但要求 base 必须在同一个 pack 里（且物理上排在前面，所以偏移是「往回」的）。
- **REF_DELTA**：base 用哈希表示，**更灵活**（base 可以在别的 pack 里、甚至还是个松散对象），适合 thin pack（网络传输时借对方已有的对象当 base）。

无论哪种，还原一个 delta 对象都必须先拿到 base。而 base 本身也可能是个 delta，于是形成一条 **delta 链**：`delta → delta → … → 真正的完整对象（base）`。读取时必须「向下钻孔」到链底的完整对象，再「向上」逐层打补丁还原。

#### 4.3.2 核心流程

delta 的**生成**（写入侧）用 `delta.h` 的 `diff_delta(src, trg)`：以 src 为基础、trg 为目标，算出一段 delta 字节流。还原用 `patch_delta(src, delta)`：把 delta 应用到 src 上重建 trg。

读取一个可能是 delta 的对象，由 `unpack_entry` 完成，分三阶段：

1. **阶段一（向下钻孔）**：从目标对象开始，若它是 delta，记录当前层信息后跳到 base，重复，直到遇到一个非 delta 的完整对象（或命中 `delta_base_cache` 缓存）。
2. **阶段二（处理 base）**：把链底的完整对象解压出来（`unpack_compressed_entry`）。
3. **阶段三（向上还原）**：沿记录的链**逆序**，对每一层用 `patch_delta` 把补丁应用到当前数据上，逐层重建，直到还原出最外层目标对象。

base 偏移的解析（`get_delta_base`）：

- `OFS_DELTA`：读取一个变长整数，它是「当前对象偏移 − base 偏移」的差值，故 `base_offset = delta_obj_offset - 差值`。这种编码让 base 总是指向「前面」。
- `REF_DELTA`：直接读 `hashsz` 字节作为 base 的哈希，再 `find_pack_entry_one` 查出它在 pack 中的偏移。

#### 4.3.3 源码精读

**delta 接口**：

[delta.h:56-69](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/delta.h#L56-L69) — `diff_delta` 是 `create_delta_index` + `create_delta` 的便捷封装：先对源缓冲建索引，再据此对目标缓冲算 delta。

[delta.h:78-80](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/delta.h#L78-L80) — `patch_delta` 是还原：给定源数据与 delta 数据，重建出目标数据。

[delta.h:89-102](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/delta.h#L89-L102) — `get_delta_hdr_size` 解析 delta 流开头的两个变长整数（源大小、目标大小），delta 流格式由它定义。

**解析 base 偏移**的核心函数，两个分支一目了然：

[packfile.c:1005-1044](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1005-L1044) — `get_delta_base`：
- `OBJ_OFS_DELTA` 分支（行 1020-1034）：变长解码出一个「回退量」，再 `base_offset = delta_obj_offset - 回退量`，并校验结果在合法范围内（`base_offset <= 0` 或 `>= delta_obj_offset` 都算越界）。
- `OBJ_REF_DELTA` 分支（行 1035-1040）：直接读 `hashsz` 字节作 base 哈希，调用 `find_pack_entry_one` 换算成偏移。

**三阶段读取**的主循环：

[packfile.c:1516-1603](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1516-L1603) — `unpack_entry` 的「PHASE 1」：
- 行 1539-1547：先查 `delta_base_cache`，命中就直接拿到已还原的 base，省掉整条链的重复工作。
- 行 1549-1571：可选地用 idx 里的 CRC32 校验本层压缩数据。
- 行 1573-1575：`unpack_object_header` 解出类型；若不是 delta 就跳出钻孔。
- 行 1577-1602：若是 delta，用 `get_delta_base` 找到 base 偏移，把**当前层**的信息（偏移、读指针、size）压入 `delta_stack`，然后跳到 base 继续。

阶段二（行 1605-1623）处理链底完整对象，阶段三（行 1625 起）从栈里逐层弹出、用 `patch_delta` 还原。`delta_stack` 让「向下钻孔」与「向上还原」共用一份记录，避免递归。

**类型回溯**还有一个辅助函数，用来在不知道完整内容时只判定 delta 对象的最终类型：

[packfile.c:1099-1165](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1099-L1165) — `packed_to_object_type` 同样沿 delta 链向上找，直到遇到一个非 delta 类型（commit/tree/blob/tag），就是这条链的最终对象类型；遇到坏对象时还有 `unwind` 回退逻辑尝试恢复。

#### 4.3.4 代码实践

**实践目标**：构造一条明显的 delta 链，观察 `OFS_DELTA` 在 `verify-pack` 输出里的样子。

**操作步骤**：

```bash
mkdir /tmp/delta-demo && cd /tmp/delta-demo
git init -q

# 1. 写一个大文件并多次提交，每次只改一点点——这是 delta 的理想素材
python3 - <<'PY'
import os
base = ["line %d" % i for i in range(2000)]
open("big.txt","w").write("\n".join(base))
PY
git add big.txt && git commit -q -m v1

for v in 2 3 4 5; do
  echo "version $v" >> big.txt
  git add big.txt && git commit -q -m "v$v"
done

# 2. 打包
git gc

# 3. 查看对象，重点看第 6 列(delta 链)与第 5 列(深度)
git verify-pack -v .git/objects/pack/*.idx | sort -k 4 -n | head -20
```

**需要观察的现象**：

- `big.txt` 的 5 个历史版本会被压成一个 base（完整对象）加若干 delta。
- `verify-pack` 输出里，delta 行的类型仍是 `blob`，但会多出「delta 深度」列（如 1、2、3…）以及「delta 基对象哈希」列，指明它相对谁做了差异。
- 用 `git cat-file -p <某个 delta blob 的哈希>` 仍能正常读出完整内容——因为 git 内部已经做了「钻孔 + 打补丁」。

**预期结果**：你亲眼看到「5 个相似大对象」被压缩成「1 个完整 + 4 个很小的 delta」，pack 体积远小于 5 份完整拷贝。把这一现象对应到 `unpack_entry` 的三阶段：阶段一沿 delta 链钻孔到 base，阶段三逐层 `patch_delta` 还原。

**待本地验证**：不同版本的 git 默认 delta 窗口与深度策略略有差异，具体哪些对象被选作 base、delta 链多深，可能与你本地的 `pack.window` / `pack.depth` 配置有关；但「相似对象被表示为 delta」这一总体行为是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把所有对象都做成 delta，delta 链越深越省空间？

**答案**：delta 链越深，读取最外层对象时「钻孔 + 打补丁」的层数越多，**读取越慢**。git 用 `pack.depth`（默认 50）限制最大链深、用 `delta_base_cache` 缓存已还原的 base 来平衡「体积」与「读取速度」。这是一个典型的时空权衡。

**练习 2**：`OBJ_OFS_DELTA` 的 base 偏移是「当前对象偏移减去一个回退量」。这种「相对偏移、只能往回指」的设计有什么好处？

**答案**：相对偏移通常很小（base 往往就在附近），用变长编码 1~3 字节就够，比 `REF_DELTA` 的 20/32 字节哈希省很多；且因为 base 必须排在前面，写入时可以流式地、按依赖顺序排放对象，读回时也保证了 base 一定先于 delta 出现，简化了拓扑。

---

## 5. 综合实践

把三个模块串起来：从一个对象哈希出发，完整复述它在源码里的「定位 → 解码 → 还原」全旅程，并对照真实文件验证。

**任务**：在复用的 `/tmp/pack-demo`（或 `/tmp/delta-demo`）仓库里完成下面三件事。

1. **定位**：任取一个对象哈希（例如某次提交的哈希，可用 `git rev-parse HEAD` 得到），然后：
   - 用 `git verify-pack -v .git/objects/pack/*.idx` 找到它在 pack 内的字节偏移。
   - 对照 [packfile.c:1734-1756](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1734-L1756)（`bsearch_pack`）与 [packfile.c:1796-1814](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1796-L1814)（`nth_packed_object_offset`），用自己的话写出「fanout 缩范围 → 哈希表二分 → 偏移表（可能跳大偏移表）」这条路径。

2. **解码**：用 `xxd -s <偏移> -l 16 .git/objects/pack/*.pack` 看这个对象在 pack 里的头几个字节，结合 [pack-write.c:500-528](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/pack-write.c#L500-L528) 的编码规则，手动解析出第一字节里的「类型」与「size 低位」。判断它是完整对象还是 delta。

3. **还原**：用 `git cat-file -p <哈希>` 正常读出内容，确认即便它是 delta 也能完整还原；再对照 [packfile.c:1516-1603](https://github.com/git/git/blob/f85a7e662054a7b0d9070e432508831afa214b47/packfile.c#L1516-L1603)（`unpack_entry` 阶段一）说明 git 内部为这次读取做了哪些钻孔与打补丁。

**验收标准**：你能不查资料地讲清楚——一个哈希进来，`bsearch_pack` 怎么用 fanout 表、偏移怎么从 32 位表（可能跳 64 位表）取出、取到的偏移如何被 `unpack_entry` 的三阶段消费、delta 链又如何被钻孔与 `patch_delta` 还原。

## 6. 本讲小结

- **pack 文件** = 12 字节文件头（`PACK` 签名 + 版本 + 对象数）+ 一串对象记录 + 末尾哈希校验尾；对象记录是「变长头（类型 3 位 + 变长 size）+ zlib 压缩内容」。
- **`.idx` 是 pack 的目录**：v2 由「魔数头 + 256 项 fanout 表 + 排序哈希表 + CRC32 表 + 32 位偏移表 + 可选 64 位大偏移表 + pack 哈希 + idx 哈希」组成。
- **fanout 表**把二分范围缩到同一首字节的桶，让按哈希查找接近 \(O(\log n)\)；`bsearch_pack` 与 `nth_packed_object_offset` 合起来完成「哈希 → 序号 → 偏移」。
- **两种 delta**：`OFS_DELTA`（相对偏移，省空间，base 必须同 pack 且在前）与 `REF_DELTA`（哈希，灵活，适合 thin pack/网络）。
- **读取 delta 对象**走「向下钻孔到完整 base → 向上逐层 `patch_delta` 还原」三阶段，并有 `delta_base_cache` 缓存与 `pack.depth` 限制链深来平衡体积与速度。
- pack 与 idx 通过**末尾校验哈希 + idx 里记录的 pack 哈希**相互绑定，`open_packed_git_1` 在打开时做交叉校验，保证两者匹配且未损坏。

## 7. 下一步学习建议

本讲解清楚了「单个 pack/idx 文件」的格式与读写。后续值得继续深入的方向：

- **多 pack 索引（multi-pack-index, `.midx`）**：当一个仓库累积了很多 pack 时，git 用 midx 把所有 pack 的对象索引合并成一张表，避免逐 pack 查找。对应 `midx.c`，将在 u13-l1「commit-graph 与 multi-pack-index」精讲。
- **pack 位图（bitmap）**：在 idx 之外再为可达对象建位图，让 clone/fetch 的对象计数从「遍历」变成「位运算」。对应 `pack-bitmap.c`，在 u13-l2。
- **网络协议里的 pack**：fetch/clone 时协商出的对象会被打成 pack 流式传输，thin pack 借用 `REF_DELTA` 引用对方已有对象。对应 `fetch-pack.c`/`send-pack.c`，在 u11。
- **pack 写入的调度策略**：哪些对象被选作 delta 的 base、delta 窗口与深度如何影响打包效果，可读 `builtin/pack-objects.c`（本讲未展开，留作进阶）。

建议先做 5 节的综合实践把本讲吃透，再进入 u4（索引 index）——那里会把「工作树 ↔ 对象数据库」之间这座桥讲透，与本讲的 pack 存储正好上下呼应。
