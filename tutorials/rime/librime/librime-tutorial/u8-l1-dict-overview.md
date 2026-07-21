# 词典系统总览与构建产物

## 1. 本讲目标

本讲是「词典系统」（u8）单元的第一篇，目标是建立一张**词典子系统全景图**，先不钻进任何一种 `.bin` 文件的字节布局。

学完后你应该能够：

- 说清楚一个 `*.dict.yaml` 源文件长什么样、它的「文件头」里有哪些设置项。
- 复述 `DictCompiler::Compile` 的四步构建（`BuildTable` / `BuildPrism` / `BuildReverseDb` / 打包），并标注每一步产出的文件名。
- 解释为什么 `.prism.bin`、`.table.bin`、`.reverse.bin` 这三种产物都继承自 `MappedFile`，以及「相对指针 `OffsetPtr`」解决了什么问题。

本讲只画地图。Prism 的双数组 trie（u8-l2）、Table 的多级索引（u8-l3）、`EntryCollector`/`Vocabulary` 的收集细节（u8-l4）、`Dictionary` 的运行期查询（u8-l5）都在后续讲义展开。

## 2. 前置知识

阅读本讲前，你最好已经具备以下认知（这些是前面讲义已经建立的）：

- **方案（Schema）与引擎流水线**（u2-l3、u6-l1）：方案的 `engine` 段装配出 Processor/Segmentor/Translator/Filter 四条链。
- **Translator 怎么查词典**（u6-l4）：`script_translator` 把输入切成 `SyllableGraph`（音节图，见 u7-l3），再交给 `Dictionary::Lookup` 查候选。
- **拼写代数**（u7-l2）：方案 `speller/algebra` 里的一串 `xform/derive/fuzz/abbrev` 规则会派生出大量「等价拼写」。

本讲要回答的问题是：**Translator 运行时查的那份「音节索引」和「词条表」，是从哪来的、谁造的、造完长什么样？** 答案就是：从人能读写的 `*.dict.yaml`，经 `DictCompiler` 编译成机器能秒级加载的 `.bin` 文件。

两个通俗概念先交代清楚：

- **源文件（source）**：`.dict.yaml`，纯文本 YAML，人写、人读、可纳入版本管理。
- **构建产物（built/prebuilt）**：`.prism.bin` / `.table.bin` / `.reverse.bin`，二进制内存映像，程序用 `mmap` 直接挂载，加载几乎零成本。

之所以要「编译」，是因为拼音/形码方案动辄几万到几十万词条，每次启动都重新解析 YAML 会非常慢。编译一次、缓存 `.bin`，运行期直接映射内存即可。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [data/minimal/luna_pinyin.dict.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.dict.yaml) | 一个真实的 `.dict.yaml` 源文件，本讲看它的「文件头」结构。 |
| [src/rime/dict/dict_settings.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc) | 解析 `.dict.yaml` 文件头的设置项（名称、版本、排序、列定义、导入表等）。 |
| [src/rime/dict/dict_compiler.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc) | 编译器主体：`Compile` 总调度，调用 `BuildTable`/`BuildPrism`/`BuildReverseDb`。 |
| [src/rime/dict/mapped_file.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h) | 内存映射文件基座，定义 `OffsetPtr`/`String`/`Array`/`List` 与 `MappedFile`。 |
| [src/rime/dict/dictionary.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc) | 产物文件扩展名的定义（`.prism.bin` / `.table.bin`），以及 `Dictionary` 如何持有这些产物。 |
| [src/rime/dict/reverse_lookup_dictionary.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/reverse_lookup_dictionary.h) | `ReverseDb`，反查库，本讲只确认它也继承 `MappedFile`。 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** `.dict.yaml` 源文件与 `DictSettings`
- **4.2** `DictCompiler`：四步编译流程
- **4.3** `MappedFile`：三种 `.bin` 产物的内存映射基座

### 4.1 `.dict.yaml` 源文件与 DictSettings

#### 4.1.1 概念说明

`*.dict.yaml` 是 librime 词典的**人类可读源格式**。一个方案（如 `luna_pinyin`）的词条都写在 `luna_pinyin.dict.yaml` 里，每行一个词条，形如「文字 + 编码（+ 权重）」。

这个文件用 YAML 的「文档」语法分成两段：

- **文件头**：被 `---` 和 `...` 包住的一小块 YAML，描述这份词典的元信息（名字、版本、排序方式、列定义、是否引入预置词集等）。
- **正文**：`...` 之后的每一行是一条词条。

把元信息和正文混在一个文件里、用 `---`/`...` 切分，是 RIME 的约定。`DictSettings` 这个类只负责**读文件头**，不碰正文——正文的解析是 `EntryCollector` 的事（u8-l4）。

为什么需要 `DictSettings` 单独存在？因为编译器在决定「要不要重建」「先收哪些文件」之前，必须先知道文件头里的设置；而读文件头比解析整份词典（几万行）便宜得多，所以拆成两步。

#### 4.1.2 核心流程

`DictSettings` 解析文件头的流程：

1. `LoadDictHeader(stream)`：逐行读输入流，直到遇到只含 `"..."` 的行（YAML 文档结束符），把这段截下来。
2. 把截下来的文本交给 `LoadFromStream`（继承自 `Config`，即 yaml-cpp 解析，见 u4-l2），变成内存里的配置树。
3. 校验 `name` 和 `version` 必须存在，否则视为「不完整的文件头」。
4. 之后用一组 getter（`dict_name()` / `sort_order()` / `use_preset_vocabulary()` / `GetTables()` / `GetColumnIndex()` 等）按需取出设置项。

其中两个 getter 值得记住：

- `GetTables()`：返回「要收集的 `.dict.yaml` 文件名列表」 = 本词典 `name` + 文件头里 `import_tables` 列出的其它词典。它决定了编译器要读几个源文件。
- `GetColumnIndex(label)`：把列标签（`text`/`code`/`weight`）映射到正文里的列号。如果文件头没有写 `columns`，就用默认 `text=0, code=1, weight=2`。

#### 4.1.3 源码精读

先看 `luna_pinyin.dict.yaml` 的文件头（[data/minimal/luna_pinyin.dict.yaml:27-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.dict.yaml#L27-L32)）：

```yaml
---
name: luna_pinyin
version: "0.12.20120711"
sort: by_weight
use_preset_vocabulary: true
...
```

四行设置：词典名 `luna_pinyin`、版本号、排序方式 `by_weight`（按权重排，另有 `original` 按原顺序）、以及引入预置词集 `essay`。注意它**没有**写 `columns`，所以正文按默认三列解析；紧接着的 `⿔	gui`（[luna_pinyin.dict.yaml:34](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.dict.yaml#L34)）就是 `text=⿔`、`code=gui`、缺省 `weight`。

再看 `LoadDictHeader` 如何截取文件头（[dict_settings.cc:15-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L15-L37)）：

```cpp
while (getline(stream, line)) {
  boost::algorithm::trim_right(line);
  header << line << std::endl;
  if (line == "...") {  // yaml doc ending
    break;
  }
}
if (!LoadFromStream(header)) { return false; }
if ((*this)["name"].IsNull() || (*this)["version"].IsNull()) {
  LOG(ERROR) << "incomplete dict header.";
  return false;
}
```

读到 `"..."` 就停，正好把正文挡在外面；随后强校验 `name`/`version`。

`GetColumnIndex` 的默认列映射（[dict_settings.cc:98-108](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L98-L108)）：

```cpp
if ((*this)["columns"].IsNull()) {
  // default
  if (column_label == "text")  return 0;
  if (column_label == "code")  return 1;
  if (column_label == "weight") return 2;
  return -1;
}
```

`GetTables` 把本词典名与 `import_tables` 拼成待收集文件清单（[dict_settings.cc:79-96](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L79-L96)），并禁止「从自己导入自己」。

`DictSettings` 的类声明在 [dict_settings.h:16-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.h#L16-L31)，它**继承自 `Config`**——也就是说，文件头本质上就是一棵普通的配置树（u4-l1 讲过的 `ConfigItem` 层次），只不过多了一组词典专用的 getter。

#### 4.1.4 代码实践

**实践目标**：亲手读懂一个 `.dict.yaml` 文件头，验证默认列映射。

**操作步骤**：

1. 打开 `data/minimal/luna_pinyin.dict.yaml`，只看前 35 行。
2. 找到 `---` 与 `...` 之间的文件头，列出所有键值对。
3. 看正文第一条词条（`⿔	gui`），数一数它有几列。
4. 对照 [dict_settings.cc:98-108](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L98-L108)，确认因为没有 `columns` 键，`text`/`code`/`weight` 分别落到第 0/1/2 列。

**需要观察的现象**：

- 文件头里**没有** `columns`，也没有 `import_tables`。
- 正文用制表符分隔，`⿔` 是第 0 列（文字），`gui` 是第 1 列（编码），第 2 列（权重）缺失。

**预期结果**：你能口头说出「这条词条的文字是 `⿔`、编码是 `gui`、权重取默认值」，并解释为什么不需要在文件头声明列。

> 说明：本实践是源码阅读型，不需要编译运行。

#### 4.1.5 小练习与答案

**练习 1**：如果一份 `.dict.yaml` 想让「编码」写在第 0 列、「文字」写在第 1 列，文件头应该怎么写？

**参考答案**：在文件头加一个 `columns` 列表，例如：

```yaml
columns:
  - code
  - text
  - weight
```

这样 `GetColumnIndex("code")` 会走 [dict_settings.cc:109-116](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L109-L116) 的分支，按列表里出现的位置返回列号（`code→0, text→1`）。

**练习 2**：`use_preset_vocabulary: true` 会影响校验和计算吗？

**参考答案**：会。[dict_compiler.cc:74-76](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L74-L76) 显示，若 `use_preset_vocabulary()` 为真，预置词集文件（默认 `essay.txt`）也会被纳入 `dict_file_checksum`。也就是说，预置词集一更新，词典就会被判定为「需要重建」。

---

### 4.2 DictCompiler：四步编译流程

#### 4.2.1 概念说明

`DictCompiler` 是把 `.dict.yaml` 变成 `.bin` 产物的**总指挥**。它本身不做底层的「拆音节」「写双数组 trie」脏活——那些是 `EntryCollector`、`Prism`、`Table`、`ReverseDb` 的事；`DictCompiler` 负责的是**编排**：先收词条、再建表、再建音节索引、再建反查库，并用校验和决定哪些步骤可以跳过。

它对外只露一个方法 `Compile(schema_file)`，内部拆成三个私有 `Build*` 方法加上一个「打包」循环。注意本讲的「四步」是指**逻辑阶段**，不是四个并列调用：

1. **`BuildTable`**（主表，`table_index==0`）：收集词条 → 生成 `.table.bin`。主表构建中会顺手调 `BuildReverseDb`。
2. **`BuildReverseDb`**：用主表的 `Vocabulary` 生成 `.reverse.bin`（反查库，供「按文字查编码」用）。
3. **`BuildPrism`**：把拼写代数应用到音节表上 → 生成 `.prism.bin`（音节索引）。
4. **打包（packs）**：对方案 `translator/packs` 里声明的每个附加词典，再各跑一次 `BuildTable`，产出多个附加 `.table.bin`。

`DictCompiler` 还有一组 `Options`（[dict_compiler.h:27-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h#L27-L32)）：`kRebuildPrism`、`kRebuildTable`、`kRebuild = 两者或`、`kDump`（把中间结果 dump 成文本，便于调试）。这些选项可以**强制**重建，绕过校验和的「能省则省」逻辑。

#### 4.2.2 核心流程

`Compile(schema_file)` 的决策流程可以画成下面这样：

```
解析 luna_pinyin.dict.yaml 文件头 (DictSettings)
   │
   ├─ GetTables()  →  得到待收集的 .dict.yaml 清单
   ├─ compute_dict_file_checksum()  →  dict_file_checksum（含 essay）
   └─ Checksum(schema_file)         →  schema_file_checksum（方案的拼写代数）
   │
   ├─ 比较 .table.bin 里存的 checksum  →  rebuild_table ?
   ├─ 比较 .prism.bin  里存的 checksum  →  rebuild_prism ?
   └─ 比较 .reverse.bin 里存的 checksum →  不一致则 rebuild_table = true
   │
   ├─ 若 rebuild_table: BuildTable(0, ...)   ──▶ luna_pinyin.table.bin
   │                                            └─▶ BuildReverseDb ──▶ luna_pinyin.reverse.bin
   ├─ 若 rebuild_prism:  BuildPrism(...)      ──▶ luna_pinyin.prism.bin
   └─ 对每个 pack:       BuildTable(i, ...)   ──▶ <pack>.table.bin
```

两条核心设计：

- **校验和驱动的增量重建**：每种 `.bin` 文件头部都存了生成它时所用的 `dict_file_checksum`（以及 prism 还多存一个 `schema_file_checksum`）。下次编译时，`DictCompiler` 把「当前源文件的校验和」与「`.bin` 里记的旧校验和」相比，一致就复用、不一致才重建。这样改了方案拼写代数只会触发 prism 重建，改了词条才会触发 table 重建。
- **源/目标分离**：`.dict.yaml` 从「源目录」（shared/user data）读，`.bin` 写到「目标目录」（staging）。`relocate_target` 函数把源文件名搬到目标目录，实现「读用户配置、写构建产物」的分离。

主表权重写入时还做了一个**对数变换**（[dict_compiler.cc:257](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L257)）：

\[ w = \ln(\max(n,\ \varepsilon)) \]

其中 \(n\) 是词条原始权重（频次），\(\varepsilon\) 是 `DBL_EPSILON`（防止 \(\ln 0\)）。这一步把「频次」翻译成「对数权重」，和 u7 系列讲的可信度一样工作在对数空间，方便后续相加合并。

#### 4.2.3 源码精读

**构造与资源解析器**（[dict_compiler.cc:27-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L27-L35)）：`DictCompiler` 从 `Dictionary` 那里借来 `dict_name_`、`packs_`、`prism_`、`tables_` 四个引用，并创建两个 `ResourceResolver`——`source_resolver_` 找 `.dict.yaml`、`target_resolver_` 是 `CreateStagingResourceResolver`，专门指向 staging 目录。

```cpp
source_resolver_(Service::instance().CreateResourceResolver({"source_file", "", ""})),
target_resolver_(Service::instance().CreateStagingResourceResolver({"target_file", "", ""})) {}
```

**校验和与重建判定**（[dict_compiler.cc:97-125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L97-L125)）：先算出两个 checksum，再分别与 `.table.bin`、`.prism.bin` 里存的旧值比对，得出 `rebuild_table`、`rebuild_prism` 两个布尔。随后一段额外检查 `.reverse.bin`（[dict_compiler.cc:129-138](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L129-L138)），若反查库缺失或 checksum 不符，会把 `rebuild_table` 也置真——因为反查库是从主表派生的，主表不重建就没法重建反查库。

**四步的主干**（[dict_compiler.cc:146-205](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L146-L205)）：

```cpp
if (rebuild_table) {
  EntryCollector collector;
  if (!BuildTable(0, collector, &settings, dict_files, dict_file_checksum)) return false;
  syllabary = std::move(collector.syllabary);
}  // ... 省略复用旧 syllabary 的分支
if (rebuild_prism && !BuildPrism(schema_file, dict_file_checksum, schema_file_checksum))
  return false;
for (int table_index = 1; table_index < tables_.size(); ++table_index) {
  // 对每个 pack 再跑一次 BuildTable
  ...
  if (rebuild_pack) { BuildTable(table_index, collector, &settings, dict_files, pack_file_checksum); }
}
```

这就是「四步」在源码里的落点：① `BuildTable(0,...)`，②（藏在 `BuildTable` 里的）`BuildReverseDb`，③ `BuildPrism`，④ pack 循环里的 `BuildTable(i,...)`。

**`BuildTable` 内部**（[dict_compiler.cc:216-278](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L216-L278)）：核心是「把每个 `RawDictEntry` 的拼写串映射成 `SyllableId` 序列，挂进 `Vocabulary`，最后由 `Table::Build + Save` 落盘」。

```cpp
map<string, SyllableId> syllable_to_id; SyllableId syllable_id = 0;
for (const auto& s : collector.syllabary) syllable_to_id[s] = syllable_id++;
for (const auto& r : collector.entries) {
  Code code;
  for (const auto& s : r->raw_code) code.push_back(syllable_to_id[s]);
  ...
  e->weight = log(r->weight > 0 ? r->weight : DBL_EPSILON);  // 对数权重
  ls->push_back(e);
}
table->Remove();
if (!table->Build(collector.syllabary, vocabulary, collector.num_entries, dict_file_checksum) ||
    !table->Save()) return false;
```

注意末尾：只有 `table_index == 0`（主表）才继续调 `BuildReverseDb`（[dict_compiler.cc:273-275](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L273-L275)），pack 表不建反查库。

**`BuildReverseDb`**（[dict_compiler.cc:280-294](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L280-L294)）：目标路径直接拼成 `dict_name_ + ".reverse.bin"`，把 syllabary、vocabulary、stems 交给 `ReverseDb::Build`，再 `Save`。

```cpp
auto target_path = target_resolver_->ResolvePath(dict_name_ + ".reverse.bin");
ReverseDb reverse_db(target_path);
if (!reverse_db.Build(settings, collector.syllabary, vocabulary, collector.stems, dict_file_checksum) ||
    !reverse_db.Save()) { ... return false; }
```

**`BuildPrism`**（[dict_compiler.cc:296-367](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L296-L367)）：先从主表读出 `syllabary`（音节全集），再读方案的 `speller/algebra`，用 `Projection::Apply(&script)`（u7-l2）把拼写代数作用上去，得到一棵带派生拼写的 `Script`，最后 `Prism::Build + Save`：

```cpp
auto algebra = config.GetList("speller/algebra");
if (algebra && p.Load(algebra)) {
  for (const auto& x : syllabary) script.AddSyllable(x);
  if (!p.Apply(&script)) script.clear();
}
prism_->Remove();
prism_->Build(syllabary, script.empty() ? nullptr : &script, dict_file_checksum, schema_file_checksum);
prism_->Save();
```

这里就接上了 u7：prism 里存的不仅是原始拼写，还有代数派生出的模糊音/缩写，所以运行期输入模糊音也能命中。

#### 4.2.4 代码实践

**实践目标**：把 `DictCompiler::Compile` 的四步与各自的输出文件名一一对应起来。这正是本讲的核心练习。

**操作步骤**：

1. 打开 [src/rime/dict/dict_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc)，定位 `Compile`（L80）、`BuildTable`（L216）、`BuildReverseDb`（L280）、`BuildPrism`（L296）。
2. 对照下面的表格，逐行确认「调用点 → 产出文件」：

   | 阶段 | 调用 | 产出文件 | 源码位置 |
   | --- | --- | --- | --- |
   | ① 主表 | `BuildTable(0, ...)` | `<dict>.table.bin` | [L216-278](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L216-L278) |
   | ② 反查库 | `BuildReverseDb(...)`（在 ① 末尾调用） | `<dict>.reverse.bin` | [L280-294](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L280-L294) |
   | ③ 音节索引 | `BuildPrism(...)` | `<dict>.prism.bin` | [L296-367](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L296-L367) |
   | ④ 附加包 | `BuildTable(i, ...)`（i≥1，pack 循环） | `<pack>.table.bin` | [L162-205](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L162-L205) |

3. 再去 [dictionary.cc:411-413](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L411-L413) 确认 `.prism.bin` / `.table.bin` 这两个扩展名是由 `ResourceType` 常量定义的：

   ```cpp
   static const ResourceType kPrismResourceType = {"prism", "", ".prism.bin"};
   static const ResourceType kTableResourceType = {"table", "", ".table.bin"};
   ```

**需要观察的现象**：

- `BuildTable` 与 `BuildPrism` 都通过 `relocate_target(...)` 把产物写到 **staging 目标目录**，而不是源文件所在目录。
- 反查库的扩展名是直接拼字符串 `".reverse.bin"`（[dict_compiler.cc:285](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L285)），不走 `ResourceType` 常量（查找时才用，见 [dict_compiler.cc:132](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L132)）。

**预期结果**：你能复述「主表+反查库一起建、prism 单独建、pack 各建自己的 table」，并说出每一步对应的文件后缀。

> 说明：本实践是源码阅读型，不运行命令。若你已在本地构建 librime 并部署过 `luna_pinyin`，可在构建目录的 staging/prebuilt 下找到这三个 `.bin` 文件加以印证（**待本地验证**）。

#### 4.2.5 小练习与答案

**练习 1**：只改了方案的 `speller/algebra`（没改词条），`Compile` 会重建哪些产物？

**参考答案**：只重建 `.prism.bin`。因为 `BuildPrism` 用 `schema_file_checksum` 判定（[dict_compiler.cc:120-121](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L120-L121)），而 `BuildTable` 用 `dict_file_checksum` 判定（[L106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L106)）。改拼写代数只动 schema 文件，`dict_file_checksum` 不变，所以 table 与 reverse 都复用。

**练习 2**：为什么 `BuildReverseDb` 只在 `table_index == 0` 时调用，pack 表不建反查库？

**参考答案**：反查库的语义是「按文字反查主词典的编码」，面向的是方案的主词典；pack 是附加词条包（如扩展词库），不需要独立反查入口。源码见 [dict_compiler.cc:273-275](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L273-L275) 的 `if (table_index == 0 && ...)`。

**练习 3**：`set_options(kRebuild)` 会带来什么效果？

**参考答案**：`kRebuild = kRebuildPrism | kRebuildTable`（[dict_compiler.h:30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h#L30)）。在 [dict_compiler.cc:139-144](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L139-L144) 里会强制把 `rebuild_table`、`rebuild_prism` 都置真，绕过校验和复用逻辑，全量重建。

---

### 4.3 MappedFile：三种 .bin 产物的内存映射基座

#### 4.3.1 概念说明

上一步产出的 `.prism.bin` / `.table.bin` / `.reverse.bin` 都是**二进制内存映像**：文件在磁盘上的字节布局，和程序把它 `mmap` 进内存后看到的布局**完全一致**。换句话说，这些文件就是「把一段精心设计的内存结构直接 dump 到磁盘」。

要做到这一点，需要一个基座类 `MappedFile`，它提供两类能力：

- **文件生命周期管理**：`Create`（建空文件并映射可写）、`OpenReadOnly`/`OpenReadWrite`（映射已有文件）、`Allocate`（在文件末尾分配并对齐一块空间）、`Resize`/`ShrinkToFit`（扩容/收缩）、`Flush`/`Save`（落盘）、`Close`/`Remove`。
- **磁盘上的可寻址数据结构**：`OffsetPtr`（相对指针）、`String`、`Array`、`List`——这些结构存的是「相对偏移」而非绝对地址，所以同一份文件在不同进程、不同地址加载都能正确寻址。

三种产物都继承自 `MappedFile`：

- `Prism : public MappedFile`（[prism.h:67](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h#L67)）
- `Table : public MappedFile`（[table.h:191](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L191)）
- `ReverseDb : public MappedFile`（[reverse_lookup_dictionary.h:40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/reverse_lookup_dictionary.h#L40)）

这就是为什么本讲把它们归为「同一类产物」——它们共享同一套序列化地基。

#### 4.3.2 核心流程

**构建期**（`DictCompiler` 调用各产物的 `Build + Save`）：

```
New<Table>(target_path)            // 绑定目标文件路径
  └─ table->Create(capacity)       // 建空文件、mmap 可写
       └─ Allocate<TableHead>()    // 在文件末尾分配并对齐一块，返回指针
            └─ CreateString(...)/CopyString(...)   // 写入字符串
       └─ ... 不断 Allocate 追加 ...
  └─ table->Save()                 // Flush 落盘 + ShrinkToFit 裁到实际大小
```

**运行期**（`Dictionary::Load` 调用各产物的 `Load`）：

```
table->OpenReadOnly()              // mmap 只读
  └─ Find<TableHead>(offset)       // 按偏移取结构
       └─ head->index_offset->...   // OffsetPtr 自动解引用成真实地址
```

关键魔法在 `OffsetPtr`：它存的不是指针，而是「目标地址相对于**自身字段**的偏移量」。解引用时用「自己字段的地址 + 偏移」算出目标地址。这样无论文件被映射到哪个虚拟地址，相对关系都不变。

#### 4.3.3 源码精读

**`OffsetPtr`**（[mapped_file.h:21-51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L21-L51)）——整个体系的灵魂：

```cpp
T* get() const {
  if (!offset_) return NULL;
  return reinterpret_cast<T*>((char*)&offset_ + offset_);
}
Offset to_offset(const T* ptr) const {
  return ptr ? (char*)ptr - (char*)(&offset_) : 0;
}
```

注意 `get()` 用的是 `(char*)&offset_ + offset_`——基准点是「`offset_` 这个字段自己的地址」，不是文件开头、不是对象开头。这种「自相对」寻址让任意两个落盘字段之间都能用偏移互指。注释也点明了它的代价：`offset_ == 0` 被用来表示空指针，所以 `OffsetPtr` **不能指向自己**（[mapped_file.h:20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L20)）。

**三个配套结构**：

- `String`（[mapped_file.h:53-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L53-L58)）：一个 `OffsetPtr<char> data` 指向以 `\0` 结尾的字符序列，就是磁盘上的字符串。
- `Array<T>`（[mapped_file.h:60-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L60-L68)）：`size` + 内联数组（`T at[1]` 是 C 风格柔性数组），数组紧跟在 size 后面。
- `List<T>`（[mapped_file.h:70-78](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L70-L78)）：`size` + `OffsetPtr<T> at`，数组不在本地、由偏移指向别处。

**`MappedFile` 类骨架**（[mapped_file.h:84-128](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L84-L128)）：protected 区域是给子类用的文件操作（`Create`/`OpenReadOnly`/`OpenReadWrite`/`Flush`/`Resize`/`ShrinkToFit`/`Allocate`/`CreateArray`/`CreateString`/`CopyString`），public 区域是查询（`Exists`/`IsOpen`/`Close`/`Remove`/`Find`/`file_path`/`file_size`）。它不可拷贝（[L110-111](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L110-L111)），因为持有 mmap 映射，拷贝会引发所有权混乱。

**`Allocate` 的自动扩容**（[mapped_file.h:134-152](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L134-L152)）：

```cpp
size_t used_space = RIME_ALIGNED(size_, T);          // 对齐到 T 的对齐要求
size_t required_space = sizeof(T) * count;
if (used_space + required_space > file_size) {       // 不够就倍增扩容
  size_t new_size = (std::max)(used_space + required_space, file_size * 2);
  if (!Resize(new_size) || !OpenReadWrite()) return NULL;
}
T* ptr = reinterpret_cast<T*>(address() + used_space);
std::memset((void*)ptr, 0, required_space);          // 清零
size_ = used_space + required_space;
return ptr;
```

`RIME_ALIGNED` 宏（[mapped_file.h:132](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L132)）把当前已用空间向上取整到 `T` 的对齐倍数，保证每个结构体都落在合法对齐边界上——这是磁盘结构能被直接 `reinterpret_cast` 使用的前提。

#### 4.3.4 代码实践

**实践目标**：亲手算一次 `OffsetPtr` 的解引用，理解「相对偏移」为何能跨地址工作。

**操作步骤**：

1. 打开 [mapped_file.h:40-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/mapped_file.h#L40-L49)，读 `get()` 与 `to_offset()`。
2. 做一个纸笔推演：假设某 `OffsetPtr<int>` 字段 `offset_` 在文件被映射后的虚地址 `0x1000` 处，`offset_` 的值是 `0x18`。问：它指向的 `int` 落在哪个地址？
3. 再回答：如果同一份文件被另一个进程映射到 `0x5000`（该字段随之落到 `0x5018`），`offset_` 仍是 `0x18`，指向的地址变成多少？指向的内容是否一致？

**需要观察的现象**：

- 第 2 步：目标地址 = `0x1000 + 0x18 = 0x1018`。
- 第 3 步：目标地址 = `0x5018 + 0x18 = 0x5030`，指向的内容与第 2 步**相同**（都是文件里偏移 `0x18` 处的那个 `int`）。

**预期结果**：你发现「基准点是字段自己」使得偏移量与映射地址无关——这正是 `.bin` 文件能被任意进程直接 `mmap` 使用的根本原因。

> 说明：本实践为纸笔推演，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `OffsetPtr` 用「相对于自身字段的偏移」，而不是「相对于文件开头的偏移」？

**参考答案**：自相对偏移在**写入时**最方便——`to_offset(ptr)` 只需 `目标地址 - 自己的地址`，不需要知道文件基址，也不依赖结构体在文件里的绝对位置。一旦结构体被整体搬移（比如插入了新字段），自相对偏移仍然有效，而绝对偏移会全部失效。

**练习 2**：`Allocate` 为什么要先 `RIME_ALIGNED(size_, T)` 再分配？

**参考答案**：为了让新分配的 `T` 落在满足 `alignof(T)` 的地址上。C++ 对象必须按其对齐要求存放，否则 `reinterpret_cast` 后的访问是未定义行为。`RIME_ALIGNED` 把已用空间向上取整到对齐倍数，保证磁盘结构与内存结构在对齐上一致。

**练习 3**：`ReverseDb` 的 `Metadata`（[reverse_lookup_dictionary.h:23-33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/reverse_lookup_dictionary.h#L23-L33)）里有两个 `OffsetPtr<char>` 字段 `key_trie`/`value_trie`，它们存的是绝对地址吗？

**参考答案**：不是。因为类型是 `OffsetPtr<char>`，存的是相对偏移；运行期经 `OffsetPtr::get()` 解析成真实地址。同一结构里还有 `List<StringId> index`，其 `at` 也是 `OffsetPtr`。整个 `Metadata` 完全由相对偏移串联，可以安全落盘。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张「词典编译全链路」梳理任务。

**任务**：以 `luna_pinyin` 方案为例，写一份「源文件 → 编译步骤 → 产物」的对照说明，要求：

1. **源文件层**：列出 `DictCompiler` 会去读哪些 `.dict.yaml`。提示——`luna_pinyin.dict.yaml` 的文件头里 `use_preset_vocabulary: true`（[luna_pinyin.dict.yaml:31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.dict.yaml#L31)），加上 `GetTables()` 的逻辑（[dict_settings.cc:79-96](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L79-L96)），说清楚「主词典 + import_tables + 预置词集 essay」三者如何进入 `dict_file_checksum`。
2. **编译步骤层**：按 `Compile` 的实际调用顺序，画出 `BuildTable(0) → BuildReverseDb → BuildPrism → pack 循环` 的时序，并在每个节点标注对应的 [dict_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc) 行号。
3. **产物层**：用一张表总结三种产物的「扩展名 / 定义位置 / 基座类 / 运行期用途」：

   | 产物 | 扩展名 | 扩展名定义位置 | 基座类 | 运行期用途 |
   | --- | --- | --- | --- | --- |
   | Prism | `.prism.bin` | [dictionary.cc:411](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L411) | `MappedFile` | 音节索引，把输入串切成 syllable_id（u8-l2） |
   | Table | `.table.bin` | [dictionary.cc:413](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L413) | `MappedFile` | 码表，按 syllable_id 序列查词条（u8-l3） |
   | ReverseDb | `.reverse.bin` | [dict_compiler.cc:285](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L285) | `MappedFile` | 反查，按文字查编码（u8-l5） |

4. **进阶思考**：如果用户只想「强制全量重建一次」，应该调用 `set_options(?)` 传什么值？为什么这会同时影响 prism 和 table？（提示：[dict_compiler.h:27-32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h#L27-L32) 与 [dict_compiler.cc:139-144](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L139-L144)）

**预期产出**：一份图文结合的笔记，包含一张源文件清单、一张编译时序图、一张产物对照表。完成后，你应当能在不看源码的情况下，回答「`luna_pinyin` 的三个 `.bin` 分别由哪段代码、在什么条件下生成」。

> 说明：本综合实践为源码阅读与文档型任务，不涉及编译运行。若本地已构建 librime，可额外运行一次部署（如通过 `rime_api_console` 触发 `start_maintenance`），在 staging 目录下实际观察到这三个 `.bin` 文件被生成——这部分**待本地验证**。

## 6. 本讲小结

- librime 词典以 `.dict.yaml` 为**人类可读源**，经 `DictCompiler` 编译成三种 **`.bin` 内存映像产物**，运行期由 `mmap` 直接挂载，避免每次启动都解析几万行 YAML。
- `.dict.yaml` 用 `---`/`...` 把文件头与正文分开；`DictSettings` 只读文件头，提供 `GetTables()`（待收集文件清单）、`GetColumnIndex()`（列映射，默认 `text/code/weight = 0/1/2`）等 getter，它本身继承自 `Config`。
- `DictCompiler::Compile` 的四步是：**`BuildTable`**（主表 → `.table.bin`，内含对数权重 \(w=\ln(\max(n,\varepsilon))\)）→ **`BuildReverseDb`**（主表派生 → `.reverse.bin`）→ **`BuildPrism`**（拼写代数作用后的音节索引 → `.prism.bin`）→ **pack 循环**（每个附加包 → 各自 `.table.bin`）。
- 重建是**校验和驱动**的：`.bin` 头部存有 `dict_file_checksum`（prism 还多存 `schema_file_checksum`），改词条触发 table 重建、改拼写代数只触发 prism 重建；`kRebuild` 选项可强制全量重建。
- 源文件从「源目录」读、`.bin` 写到「staging 目标目录」，由 `relocate_target` 搬运文件名，实现读写分离。
- 三种产物 `Prism`/`Table`/`ReverseDb` 都继承自 `MappedFile`，共享 `OffsetPtr`（自相对偏移指针）、`String`/`Array`/`List` 与 `Allocate`（对齐 + 倍增扩容）这套序列化地基——这是 `.bin` 文件能被任意进程直接 `mmap` 使用的根本。

## 7. 下一步学习建议

本讲只画了词典子系统的「外围地图」。接下来按依赖顺序深入：

- **u8-l2 Prism**：拆开 `.prism.bin`，看它如何基于双数组 trie（Darts）把拼写映射到 `syllable_id`，以及 `CommonPrefixSearch`/`ExpandSearch` 等查询接口。
- **u8-l3 Table**：拆开 `.table.bin`，看 `HeadIndex`/`TrunkIndex`/`TailIndex` 多级索引如何实现「音节序列 → 词条集合」。
- **u8-l4 DictCompiler 构建流程（细）**：聚焦 `EntryCollector` 三遍收集、`Vocabulary` 的 `map<int, VocabularyPage>` 树形组织、`checksum` 校验细节——本讲里被视为黑盒的 `collector.Collect(dict_files)` 在这里展开。
- **u8-l5 Dictionary 查询主链路**：从运行期视角，看 `SyllableGraph → Prism(取 id) → Table(查词条) → DictEntryCollector` 这条查询链如何把本讲构建的产物用起来。

建议阅读顺序：u8-l2 → u8-l3 → u8-l4 → u8-l5。读 u8-l2/u8-l3 时，可以不断回头对照本讲的「产物对照表」，把每种 `.bin` 的内部结构和它的生成步骤对应起来。
