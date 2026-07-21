# Prism：音节索引（双数组 Trie）

## 1. 本讲目标

本讲是词典系统（u8）的第二篇，承接 u8-l1 建立的全景图。u8-l1 告诉我们：用户词典的「码 → 词」查询要靠三种 `.bin` 产物，其中 **Prism** 负责把一段拼写（如拼音 `ni`、模糊音 `li`、缩写 `n`）快速映射到一个 `syllable_id`。

读完本讲，你应该能够：

1. 说清 Prism 在磁盘上的序列化结构（`Metadata` + 双数组 trie 镜像 + `SpellingMap`），以及它如何复用 `MappedFile` 的内存映射基座。
2. 解释 `Prism::Build` 如何把「拼写代数（algebra）产物」编译进 trie，从而让模糊音、缩写、纠错等**派生拼写**也拥有自己的索引条目。
3. 区分四个查询接口 `GetValue` / `HasKey` / `CommonPrefixSearch` / `ExpandSearch` 的用途，并知道运行期是谁在调用它们。
4. 理解 `SpellingMap` 与 `SpellingAccessor` 如何把「一条派生拼写」反向解出「它对应哪些原始音节」，这是模糊音能查到正确词条的关键。

---

## 2. 前置知识

- **音节（syllable）与音节表（Syllabary）**：一个方案里所有合法拼音音节的集合，类型是 `set<string>`（见 [vocabulary.h:L17](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L17)）。每个音节在构建期被分配一个整数编号 `SyllableId`（`int32_t`）。
- **拼写代数（Spelling Algebra）**：方案 `speller/algebra` 里的一串规则（`xform/derive/erase/fuzz/abbrev/...`），会把原始音节派生出多种拼写变体。详见 u7-l2《Calculus 与拼写运算》。代数的展开产物是一个 `Script`（`map<string, vector<Spelling>>`，键是拼写、值是该拼写对应的所有音节拼写），见 [algebra.h:L20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/algebra.h#L20)。
- **Trie（前缀树/字典树）**：一种把「变长字符串集合」组织成树的数据结构，公共前缀共享路径，查找时间与串长成正比、与集合大小无关。
- **双数组 Trie（Double-Array Trie）**：用两片连续数组（Darts 实现里通常合并为一片 `array[]`）紧凑编码 trie，查找仍是 \(O(L)\)（\(L\) 为键长），但内存占用与缓存友好度远优于指针版 trie。本仓库自带 header-only 实现 `include/darts.h`。
- **内存映射文件（mmap）**：把磁盘文件直接映射进进程地址空间，`.bin` 里的结构体可以像内存对象一样被读写，无需反序列化。librime 用 `MappedFile` 封装它，详见 u8-l1。

> 一句话定位：**Prism 是一张「拼写串 → syllable_id」的双数组 trie，附带一张「拼写 id → 它对应的若干原始音节」的反查表（SpellingMap）。** 前者负责「这段输入能匹配上哪些拼写」，后者负责「这些拼写分别对应哪些音节」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/dict/prism.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h) | Prism 的声明：磁盘结构体（`Metadata`/`SpellingDescriptor`/`SpellingMap`）、`SpellingAccessor` 迭代器、`Prism` 类的 `Build` 与查询接口。本讲的核心文件。 |
| [src/rime/dict/prism.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc) | 上述声明的实现：构建、查询、`SpellingAccessor` 解包逻辑。 |
| [src/rime/dict/mapped_file.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h) | `MappedFile` 基座，提供 `Allocate`/`CreateArray`/`OffsetPtr`/`String`/`Array`/`List` 等序列化原语。Prism、Table、ReverseDb 都继承自它。 |
| [src/rime/dict/dict_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc) | `BuildPrism`：从主表取音节表、应用拼写代数生成 `Script`、调用 `Prism::Build`。 |
| [src/rime/algo/syllabifier.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc) | 运行期调用方之一：用 `CommonPrefixSearch`/`ExpandSearch`/`QuerySpelling` 切音节图。 |
| [src/rime/dict/dictionary.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc) | 运行期调用方之二：用 `GetValue`/`ExpandSearch` 做按字符串反查词条。 |

---

## 4. 核心概念与源码讲解

本讲把 Prism 拆成四个最小模块：① 磁盘序列化结构、② `Build` 构建流程、③ 四个查询接口、④ `SpellingMap` 与 `SpellingAccessor`。

### 4.1 双数组 Trie 与 Prism 的序列化结构

#### 4.1.1 概念说明

Prism 落盘后是一个 `.prism.bin` 文件。这个文件不是「一个 trie 对象」的 dump，而是 `MappedFile` 按需 `Allocate` 出来的一片连续内存，里面依次摆放三类东西：

1. **`Metadata`**：文件头，记录格式版本、校验和、音节/拼写数量、双数组大小，以及指向双数组和拼写表的两个**相对偏移指针** `OffsetPtr`。
2. **双数组镜像（`double_array`）**：Darts trie 的裸字节镜像，`trie_->set_array(...)` 直接挂上去就能用。
3. **`SpellingMap`**：一张「拼写 id → 该拼写对应的若干音节描述符」的表，是模糊音/缩写能够反向找到原始音节的依据。

为什么要分两层（trie + SpellingMap）？因为双数组 trie 的「值」只能是一个整数，而一条**派生拼写**（由代数规则生成）可能同时对应**多个**原始音节。例如模糊音规则可能让 `li` 同时表示 `li` 和 `ni` 两个音节。于是 trie 的值只存一个「拼写 id」（即该拼写在 `SpellingMap` 中的下标），真正的「一对多」关系放在 `SpellingMap` 里。

#### 4.1.2 核心流程

加载一个 `.prism.bin` 的步骤（对应 `Prism::Load`）：

```
OpenReadOnly()                    # mmap 整个文件
  ↓
Find<Metadata>(0)                 # 文件头在偏移 0
  ↓
校验 format 前缀 == "Rime::Prism/"  # 否则认作损坏
  ↓
format_ < 4.0 ?                   # 版本太旧则强制重建（返回 false）
  ↓
trie_->set_array(metadata->double_array.get(), size)   # 把裸字节当 trie 用
  ↓
spelling_map_ = metadata->spelling_map.get()            # 取拼写表指针
```

关键点：`OffsetPtr` 是**自相对偏移**——它存的是「目标地址相对于自己这个字段地址」的字节差。所以 `.bin` 被映射到任何基地址都能正确解引用，不必做重定位。

#### 4.1.3 源码精读

`Metadata` 结构体定义了文件头布局：

[prism.h:L35-L47](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h#L35-L47) —— 定义 `format`（版本串）、两个校验和、`num_syllables`/`num_spellings`、`double_array_size` 与 `OffsetPtr<char> double_array`（双数组入口）、`OffsetPtr<SpellingMap> spelling_map`，以及构建期统计出的 `alphabet[256]`。

[prism.h:L24-L33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h#L24-L33) —— 定义单条拼写描述符 `SpellingDescriptor`（含 `syllable_id`、打包了 `type` 与 `is_correction` 的 `int32_t type`、`credibility`、`tips`），以及用 `List`/`Array` 组合出的二维表 `SpellingMap`：外层 `Array<SpellingMapItem>` 按「拼写 id」索引，每个元素是一个 `List<SpellingDescriptor>`（一对多）。

`OffsetPtr` 的「自相对偏移」实现：

[mapped_file.h:L40-L44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L40-L44) —— `get()` 用 `(char*)&offset_ + offset_` 还原目标地址；`to_offset` 则反向算出偏移。注释点明限制：`offset_ == 0` 表示 NULL，故结构体不能自指。

`Prism::Load` 的版本校验：

[prism.cc:L99-L112](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L99-L112) —— 先比 `format` 前缀，再用 `atof` 解析版本号；`format_ < kPrismFormatVersion - DBL_EPSILON`（即早于 4.0）就关闭文件返回 `false`，触发上游重建。当前版本常量定义在 [prism.cc:L29-L30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L29-L30)。

[prism.cc:L114-L128](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L114-L128) —— 取出双数组指针并 `trie_->set_array(array, array_size)`，再（若版本 > 1.0）取出 `spelling_map_`。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：直观看到 `.prism.bin` 不是黑盒，而是有明确文件头的二进制。
2. **步骤**：
   - 打开 [prism.cc:L29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L29)，记住格式串 `"Rime::Prism/4.0"`。
   - 找到本机 RIME 的预构建目录（通常在用户配置目录的 `build/` 下），挑一个 `*.prism.bin`。
   - 用任意十六进制查看器看文件**前 32 字节**。
3. **观察**：开头应是可读的 `Rime::Prism/4.0`（可能尾部补 `\0`），紧跟 4 字节的 `dict_file_checksum`。
4. **预期结果**：前若干字节能直接读出 `Rime::Prism/`，验证「`Metadata` 位于偏移 0、`format` 是首字段」的设计。若无法在本机定位文件，可标注「待本地验证」。

#### 4.1.5 小练习与答案

- **练习**：`Metadata::double_array` 的类型是 `OffsetPtr<char>` 而非 `char*`，为什么？
- **答案**：`.bin` 被 `mmap` 到的基地址每次运行都不一定相同；裸指针 `char*` 存的是绝对地址，重开会失效。`OffsetPtr` 存的是相对自身字段的偏移（见 [mapped_file.h:L47-L49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L47-L49)），与基地址无关，故文件可直接映射使用。

---

### 4.2 Build：把拼写代数产物编译进 Trie

#### 4.2.1 概念说明

`Prism::Build` 是构建期的核心。它的输入有两个：

- `syllabary`：原始音节表（`set<string>`）。
- `script`：拼写代数展开后的产物（`Script`，即 `map<string, vector<Spelling>>`）。**没有代数时传 `nullptr`**。

如果传了 `script`，trie 的键就不再是「原始音节」，而是 `script` 里的每一条**拼写**（含代数派生出的模糊音、缩写等）。这就解释了练习任务的核心问题——**为什么模糊音 `li` 也能查到 `ni` 对应的词条**：因为代数规则在 `script` 里为音节 `ni` 生成了派生拼写 `li`，`Build` 把 `li` 也写进了 trie，并在 `SpellingMap` 里记录「拼写 `li` → 音节 `ni`（和音节 `li`）」。

#### 4.2.2 核心流程

`Build` 的步骤（对应 [prism.cc:L139-L254](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L139-L254)）：

```
1. 收集 keys[]
   - 若有 script：遍历 script，每个拼写串作为一个 key，统计 map_size（所有 Spelling 总数）
   - 若无 script：遍历 syllabary，每个原始音节作为一个 key
2. trie_->build(num_spellings, keys)        # Darts 构建双数组，键按字典序，值即下标 0..n-1
3. Create(容量估算)                          # 预留映射文件空间（镜像 + 估算的拼写表）
4. Allocate<Metadata>()，填 num_syllables/num_spellings/checksums
5. 统计 alphabet：扫描所有 key 的字符，去重排序写入 metadata->alphabet
6. Allocate<char>(image_size)，memcpy 双数组镜像，挂到 metadata->double_array
7. 若有 script：建 syllable_to_id 表，CreateArray<SpellingMapItem>(num_spellings)，
   逐条 Allocate<SpellingDescriptor> 并填 syllable_id/type/credibility/tips
8. strncpy(format, "Rime::Prism/4.0")        # 最后写版本号（保证半成品不会被当合法文件加载）
```

注意 trie 的「值」语义：Darts 的 `build` 默认把第 i 个键的值设为 `i`（即键在 `keys[]` 中的下标）。这个下标随后被用作 `SpellingMap` 的索引——查询时 `CommonPrefixSearch` 返回的 `Match.value` 就是这个下标。

`type` 字段被复用打包了两个信息：低 30 位存 `SpellingType` 枚举，第 30 位存 `is_correction` 标志。

#### 4.2.3 源码精读

收集键与构建 trie：

[prism.cc:L149-L162](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L149-L162) —— 有 `script` 时遍历 `script`（`it->first` 是拼写串），统计 `map_size = Σ 每个拼写的 Spelling 数`；否则回退到 `syllabary`。然后 `trie_->build(num_spellings, &keys[0])` 构建双数组。

统计 `alphabet`（用于后续 `ExpandSearch` 只枚举真正出现过的字符）：

[prism.cc:L187-L197](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L187-L197) —— 用 `set<char>` 去重收集所有拼写里出现的字符，按字典序写入 `metadata->alphabet`。这让补全搜索不必盲目试遍 26 个字母。

构建 `SpellingMap` 并把 `is_correction` 打包进 `type` 高位：

[prism.cc:L208-L248](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L208-L248) —— 先建 `syllable_to_id`（音节串→id，与主表编码一致），再 `CreateArray<SpellingMapItem>(num_spellings)`；对每个拼写，`Allocate<SpellingDescriptor>(list_size)` 填充：`desc->syllable_id = syllable_to_id[j->str]`（把拼写里记录的音节串翻译成 id），`type` 用掩码打包 `is_correction`，`credibility` 与 `tips` 直接复制。

打包/解包用的位掩码：

[prism.cc:L23-L25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L23-L25) —— `kTypeIsCorrectionMask = 1 << 30`（刻意避开符号位第 31 位），`kSpellingTypeMask` 取低 30 位。

调用方如何准备 `script`（即「代数如何进入 Prism」）：

[dict_compiler.cc:L311-L327](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L311-L327) —— `BuildPrism` 从主表取 `syllabary`，读方案的 `speller/algebra`，用 `Projection::Load` + `Projection::Apply(&script)` 把代数规则施加到音节表上得到 `script`；若代数应用失败则 `script.clear()`。

[dict_compiler.cc:L356-L364](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L356-L364) —— 调 `prism_->Build(syllabary, script.empty() ? nullptr : &script, ...)`。**这正是「派生拼写被纳入索引」的入口**：`script` 非空时 `Build` 走 `script` 分支，把所有派生拼写都建成 trie 键。

#### 4.2.4 代码实践（练习任务：理解模糊音/缩写如何进索引）

1. **目标**：用一段假想的 `algebra` 规则，手动推演 `script` 与最终 trie 键，说明模糊音与缩写为何能命中。
2. **步骤**：
   - 假设音节表只有 `{"ni", "li"}`。
   - 假设 `speller/algebra` 含两条规则：`derive/^n/l/`（把声母 n 换成 l，即派生出模糊音拼写）和 `abbrev/^([nl]).+$/$1/`（取首字母作缩写）。
   - 按 u7-l2 的语义推演 `script` 的键集。
3. **观察**：`script` 应包含拼写 `ni`、`li`（来自原始+派生）、以及缩写 `n`、`l`。
4. **预期结果**：`Build` 会把 `ni`、`li`、`n`、`l` 四个串都写进 trie。于是运行期用户输入 `l`（缩写）或 `li`（模糊音）时，`CommonPrefixSearch` 都能命中；命中后通过 `SpellingMap` 反查到原始音节 `ni`/`li`，再到主表查到正确词条。**这就回答了练习任务**：代数产物在构建期被「物化」成 trie 的额外键，运行期无需重新做字符串变换。
5. 若想进一步验证，可在 [dict_compiler.cc:L351-L355](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L351-L355) 看到 `kDump` 选项会把 `script` 落盘为 `.txt`，构建时加 `kDump` 即可肉眼核对派生拼写。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `metadata->format` 要在 `Build` **最后**才 `strncpy` 写入（[prism.cc:L251-L252](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L251-L252)）？
- **答案**：`Load` 用 `format` 判断文件是否合法且版本够新。若构建中途崩溃，文件头的 `format` 仍是旧值/空值，`Load` 会判定非法而拒绝加载，避免读到半成品。
- **练习 2**：若方案的 `speller/algebra` 为空，`Prism::Build` 走哪条分支？trie 的键是什么？
- **答案**：`BuildPrism` 里 `script` 为空、传 `nullptr`；`Build` 走 [prism.cc:L154-L158](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L154-L158) 的 `syllabary` 分支，trie 的键就是原始音节本身，`SpellingMap` 不构建（`spelling_map_` 为 NULL），运行期 `QuerySpelling` 返回的迭代器立即 `exhausted`。

---

### 4.3 查询接口：GetValue / HasKey / CommonPrefixSearch / ExpandSearch

#### 4.3.1 概念说明

Prism 对外暴露四个查询接口，全部委托给 Darts 的 `Darts::DoubleArray`：

| 接口 | 语义 | 典型调用方 |
| --- | --- | --- |
| `HasKey(key)` | 是否存在**完全相等**的拼写 | 构建期/诊断 |
| `GetValue(key, *value)` | 取**完全相等**拼写的值（拼写 id） | `Dictionary::LookupWords` 精确反查 |
| `CommonPrefixSearch(key, *result)` | 找出 `key` 的**所有前缀**对应的拼写 | `Syllabifier` 切音节、`TableTranslator` |
| `ExpandSearch(key, *result, limit)` | 找出**以 `key` 为前缀**的所有拼写（补全） | `Syllabifier` 的补全、`Dictionary::LookupWords` 预测 |

注意「前缀」方向相反：`CommonPrefixSearch` 是「输入比拼写长」（输入 `nihao` 能切出前缀音节 `ni`），`ExpandSearch` 是「输入比拼写短」（输入 `n` 补出 `ni`/`nan`/...）。

返回类型 `Match = Darts::DoubleArray::result_pair_type`，含 `.value`（拼写 id）与 `.length`（匹配上的字节数）。

#### 4.3.2 核心流程

**`CommonPrefixSearch`**（[prism.cc:L272-L280](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L272-L280)）：

```
result.resize(len)                        # 最多 len 个前缀
n = trie_->commonPrefixSearch(key, ..., len, len)
result.resize(n)                          # 实际命中数
```

**`ExpandSearch`**（[prism.cc:L282-L324](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L282-L324)）：先 `traverse(key)` 判断前缀本身是否是合法路径（`-2` 即非法，直接返回空）；若前缀本身是终止节点则先记一条；然后用 BFS 逐个追加 `alphabet` 里的字符继续 `traverse`，收集所有终止节点，直到达到 `limit` 或队列空。

`traverse` 的三态返回值是 `ExpandSearch` 的核心：

| `traverse` 返回 | 含义 | 处理 |
| --- | --- | --- |
| `>= 0` | 当前路径是终止节点（有值） | 记一条结果，并入队继续扩展 |
| `-1` | 路径存在但非终止 | 入队继续扩展 |
| `-2` | 路径不存在 | 剪枝，不入队 |

查找代价：理想情况下每次查询是 \(O(L)\)（\(L\) 为键长）；`ExpandSearch` 是 \(O(\text{命中数} \times \overline{\text{深度}})\)，故调用方都带 `limit` 上限（如 `kExpandSearchLimit = 512`）。

#### 4.3.3 源码精读

`Match` 类型与四个接口声明：

[prism.h:L69-L85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h#L69-L85) —— `using Match = Darts::DoubleArray::result_pair_type;`，以及 `HasKey/GetValue/CommonPrefixSearch/ExpandSearch/QuerySpelling` 的签名。

`GetValue` / `HasKey` 的实现：

[prism.cc:L256-L268](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L256-L268) —— 都用 `trie_->exactMatchSearch<int>(key.c_str())`；返回 `-1` 表示未命中（注意 Darts 约定值不能为 `-1`，故拼写 id 从 0 起、合法）。

`ExpandSearch` 的 BFS：

[prism.cc:L300-L323](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L300-L323) —— 关键细节：枚举字符时用 `(format_ > 1.0 - DBL_EPSILON) ? metadata_->alphabet : kDefaultAlphabet`，新版文件用构建期统计的真实字符集，避免无谓试探。

运行期调用方印证：

- [syllabifier.cc:L69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L69) 用 `CommonPrefixSearch` 枚举当前位置开始的所有前缀音节画边。
- [syllabifier.cc:L206-L208](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L206-L208) 在开启补全时用 `ExpandSearch`（上限 512）补出未输完的音节。
- [dictionary.cc:L308-L315](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L308-L315) 按字符串反查词条时，预测模式用 `ExpandSearch`、精确模式用 `GetValue`。

#### 4.3.4 代码实践（源码阅读型：跟踪调用链）

1. **目标**：把「一次查询」从调用方追到 Darts，看清 `Match.value` 的去向。
2. **步骤**：
   - 从 [dictionary.cc:L299](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L299) 的 `LookupWords` 读起。
   - 看精确分支 `prism_->GetValue(str_code, &match.value)`（[dictionary.cc:L312](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L312)），`match.value` 即拼写 id。
   - 紧接着 [dictionary.cc:L319](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L319) 用 `QuerySpelling(match.value)` 把拼写 id 反解成音节。
3. **观察**：`value` 是一个「拼写 id」，并非「音节 id」；要再过一次 `SpellingMap` 才得到真正的 `syllable_id`。
4. **预期结果**：能画出 `输入串 → GetValue/ExpandSearch(得拼写id) → QuerySpelling(得音节id) → 主表查词条` 的数据流。

#### 4.3.5 小练习与答案

- **练习**：为什么 `ExpandSearch` 要带 `limit` 参数，而 `CommonPrefixSearch` 不要？
- **答案**：`CommonPrefixSearch` 的结果数上界是输入长度 `len`（前缀数有限），自带 `resize(len)` 上限；而 `ExpandSearch` 是向「更深更长」扩展，命中数可能极大（补全一个字母可能匹配成千上万词条），故必须由调用方传 `limit` 截断（如 `kExpandSearchLimit = 512`），避免爆内存/拖慢响应。

---

### 4.4 SpellingMap 与 SpellingAccessor：拼写→音节的反向映射

#### 4.4.1 概念说明

trie 只能告诉我们「这段输入命中了某个拼写 id」。但下游（音节图、主表查询）要的是**音节 id**，而且一条派生拼写可能对应**多个**原始音节（模糊音的常态）。`SpellingMap` 就是补上这一环的反查表：

```
拼写 id  ──trie──▶  value(=拼写id)  ──SpellingMap──▶  [音节id + 属性, ...]
```

`SpellingAccessor` 是遍历这张表的只读迭代器：给定一个拼写 id，依次吐出 `(syllable_id, SpellingProperties)`。属性里携带 `type`（普通/模糊/缩写/补全/歧义/纠错）、`credibility`（对数可信度）、`tips`、`is_correction`——这些正是 u7-l1 讲过的拼写属性，构建期被代数算好、现在原样读回。

#### 4.4.2 核心流程

`QuerySpelling(id)` 构造一个 `SpellingAccessor`，定位到 `spelling_map->at[id]` 这个 `List` 的 `[begin, end)` 区间：

```
构造：iter_ = spelling_map->at[id].begin();  end_ = ....end()
循环：while (!accessor.exhausted()) {
        id = accessor.syllable_id();
        props = accessor.properties();
        accessor.Next();
      }
```

属性解包：`SpellingDescriptor::type` 是打包值，用 `kSpellingTypeMask` 取低 30 位还原 `SpellingType`，用 `kTypeIsCorrectionMask` 测第 30 位还原 `is_correction`。

可信度 `credibility` 工作在对数空间（u7-l1 已述）：完整拼写为 \(0\)，模糊/缩写为 \(\ln 0.5\)，补全为 \(\ln 0.05\)，纠错为 \(\ln 0.01\)。对数空间的好处是概率相乘退化为相加：

\[
\log(p_1 \cdot p_2) = \log p_1 + \log p_2
\]

所以一条多音节路径的总可信度就是各音节可信度之和，便于在音节图里比大小选最优路径。

#### 4.4.3 源码精读

`SpellingAccessor` 声明：

[prism.h:L51-L63](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h#L51-L63) —— 持有 `spelling_id_`（当前拼写 id，耗尽后置 `-1`）、`iter_`/`end_`（指向 `List<SpellingDescriptor>` 的区间）。

构造与推进：

[prism.cc:L37-L53](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L37-L53) —— 构造时若 `spelling_map` 为空或 id 越界，`iter_` 保持 NULL；`Next()` 推进 `iter_`，越过 `end_` 即把 `spelling_id_` 置 `-1` 表示耗尽。

解包属性：

[prism.cc:L66-L77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L66-L77) —— `props.type = (SpellingType)(packed_type & kSpellingTypeMask)`，`props.is_correction = (packed_type & kTypeIsCorrectionMask) != 0`，`credibility` 与非空 `tips` 直接读出。

运行期如何用 `QuerySpelling` 决定「该拼写是否可作为正常音节」：

[syllabifier.cc:L106-L127](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L106-L127) —— 遍历某命中的所有音节，`strict_spelling_` 且整串匹配时丢弃非 `kNormalSpelling` 的模糊/缩写；同一终点若多种拼写到达同一音节，用 `(std::min)(type)` 取最优（数值越小越可信，见 u7-l1 的 `SpellingType` 排序）。

[dictionary.cc:L319-L325](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L319-L325) —— 反查词条时只接受 `type <= kNormalSpelling`（即仅普通拼写），跳过模糊/缩写，避免把模糊音当精确码反查。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：看清「一对多」如何在运行期被消费。
2. **步骤**：在 [syllabifier.cc:L77-L85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L77-L85) 的纠错分支里，看 `QuerySpelling(m.first)` 循环：它找的是「是否存在一条 `type == kNormalSpelling && !is_correction` 的原始音节」，找到就 `break`。
3. **观察**：即便一条拼写由纠错器注入，它仍可能映射回一个完全正常的原始音节——`SpellingMap` 记录的是「拼写对应哪些音节」，与「拼写本身是怎么来的」无关。
4. **预期结果**：能解释「为什么纠错命中后仍能查到正确词条」——因为 `SpellingMap` 保存的是拼写→音节的客观映射，`is_correction` 只是该映射上一条记录的属性标签。

#### 4.4.5 小练习与答案

- **练习 1**：`SpellingAccessor::exhausted()` 用 `spelling_id_ == -1` 判断（[prism.cc:L55-L57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L55-L57)），而不是 `iter_ == end_`，为什么？
- **答案**：构造时 `spelling_map` 可能为 NULL（无代数的方案不建表），此时 `iter_`/`end_` 都是 NULL，`iter_ == end_` 会误判为「空但有效」。改用 `spelling_id_ == -1` 这个显式哨兵：正常时 `spelling_id_` 是非负的拼写 id，`Next()` 越界后置 `-1`，构造失败时也保持 `-1`（默认），统一表达「耗尽/无效」。
- **练习 2**：为什么 `dictionary.cc` 反查词条时只取 `type <= kNormalSpelling`，而 `syllabifier.cc` 却允许模糊/缩写？
- **答案**：两者目标不同。音节切分（syllabifier）要尽量给出所有可能的切法供后续打分择优，故接纳模糊/缩写；而 `LookupWords` 是「按用户给出的精确编码串反查词条」，模糊/缩写是代数派生出的「等价拼写」而非用户真实输入，纳入它们会污染反查结果，故只留普通拼写。

---

## 5. 综合实践

**任务**：用一张图把「模糊音输入如何变成候选词」整条链路串起来，并标注 Prism 在其中的两次出场。

请按以下步骤完成（源码阅读 + 推演，无需改源码）：

1. **准备方案配置**：找一个含模糊音规则的方案（或自行假想），其 `speller/algebra` 含一条 `derive/^n/l/`。记下原始音节表里同时存在 `ni` 和 `li`。
2. **构建期（推演）**：
   - 跟踪 [dict_compiler.cc:L311-L327](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L311-L327)：`Projection::Apply(&script)` 会给音节 `ni` 派生出拼写 `li`。
   - 跟踪 [prism.cc:L149-L162](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L149-L162)：`script` 的键 `li` 被加入 trie。
   - 跟踪 [prism.cc:L208-L248](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.cc#L208-L248)：在 `SpellingMap[li 的拼写id]` 里写入两条描述符——音节 `li`（普通）和音节 `ni`（模糊，`type=kFuzzySpelling`，`credibility=ln 0.5`）。
3. **运行期（推演）**：用户输入 `li`。
   - `Syllabifier` 调 `CommonPrefixSearch("li")`（[syllabifier.cc:L69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L69)）命中拼写 `li`，得到 `Match.value = (li 的拼写 id)`。
   - 调 `QuerySpelling(value)`（[syllabifier.cc:L106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.cc#L106)）解出两个音节 `li`、`ni`，各自带属性画进音节图。
   - 下游沿音节图到主表（Table，u8-l3）查出 `li`、`ni` 对应的词条并按可信度排序。
4. **产出**：画一张含「构建期 script→trie+SpellingMap」与「运行期 输入→CommonPrefixSearch→QuerySpelling→Table」两段的流程图，标注 Prism 的两次出场。
5. **若要真机验证**：构建时给 `DictCompiler` 加 `kDump` 选项（[dict_compiler.cc:L351-L355](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L351-L355)）导出 `script` 的 `.txt`，核对 `li` 拼写下是否同时挂了 `li` 与 `ni` 两个音节。导出步骤涉及重新构建词典，具体命令待本地验证。

---

## 6. 本讲小结

- Prism 落盘是 `MappedFile` 映射的一片连续内存：`Metadata`（文件头，含格式版本与两个校验和）+ Darts 双数组 trie 镜像 + `SpellingMap`（一对多反查表）。
- `OffsetPtr` 用「自相对偏移」存储跨结构体指针，使 `.bin` 映射到任意基地址都能直接使用；`format` 字段在 `Build` 最后写入，保证半成品不会被误加载。
- `Prism::Build` 在有 `script`（拼写代数产物）时，把**每一条派生拼写**都建成 trie 键，并在 `SpellingMap` 记录「拼写→音节」映射——这是模糊音、缩写、纠错能在运行期命中的根本原因。
- 四个查询接口分工：`GetValue`/`HasKey` 精确匹配；`CommonPrefixSearch` 找输入的所有前缀音节（切分）；`ExpandSearch` 找以输入为前缀的所有拼写（补全，带 `limit`）。
- `Match.value` 是「拼写 id」而非「音节 id」；要再经 `SpellingAccessor`（`QuerySpelling`）遍历 `SpellingMap` 才得到真正的音节与属性（`type`/`credibility`/`tips`/`is_correction`）。
- `type` 字段低 30 位存 `SpellingType`、第 30 位存 `is_correction`；`credibility` 在对数空间，使多音节路径可信度可加。

---

## 7. 下一步学习建议

- **u8-l3《Table：码表与多级索引》**：Prism 给出的是 `syllable_id` 序列，Table 才把这个序列翻译成具体词条。建议接着读 `table.h`/`table.cc`，对照本讲的「拼写 id vs 音节 id」区分，理解 Table 的 `HeadIndex/TrunkIndex/TailIndex` 如何按音节序列查词。
- **u8-l4《DictCompiler 构建流程》**：本讲多次引用 `DictCompiler::BuildPrism`，下一篇会把它放进完整的 `EntryCollector → Vocabulary → Table/Prism/ReverseDb` 构建链里讲。
- **回顾 u7-l1/u7-l2**：若对 `SpellingProperties` 的 `type`/`credibility` 计算细节（为何是 \(\ln 0.5\)、\(\ln 0.05\)）尚不清晰，可回看拼写代数两讲，本讲的 `SpellingMap` 正是它们在磁盘上的落点。
- **可选深读**：阅读 `include/darts.h` 中 `DoubleArray::build` / `commonPrefixSearch` / `traverse` 的注释，理解双数组 trie 的 `BASE`/`CHECK` 机制与 `result_pair_type` 的字段定义。
