# DictCompiler 构建流程

## 1. 本讲目标

本讲是「词典系统」单元（u8）的第四篇。上一篇 u8-l3 讲清了 `Table` 在**运行期**如何用多级数组索引回答「音节序列 → 词条」。本讲回答它的前一个问题：这些 `.table.bin` / `.prism.bin` / `.reverse.bin` 是**怎么被造出来**的。

学完本讲你应该能够：

- 说清 `DictCompiler::Compile` 把一份人类可读的 `*.dict.yaml` 变成三类二进制产物的完整步骤。
- 理解 `EntryCollector` 的「三遍收集」（Pass 1 读条目、Pass 2 编码无码词、Pass 3 合入预设词表）分别做什么，以及它如何产出 `syllabary` 与 `RawDictEntry`。
- 掌握 `Vocabulary` 的 `map<int, VocabularyPage>` 多级树形组织，并能解释 `Code::kIndexCodeMaxLength == 3` 如何决定树的深度。
- 理解校验和（checksum）如何驱动「是否需要重建」的增量决策——改词条重建 table，改拼写代数只重建 prism。
- 跟踪一个具体词条（如 `你好	ni hao`）从 `.dict.yaml` 一路写到 `.table.bin` 的全过程。

## 2. 前置知识

在进入源码前，先建立三个直觉。本讲假设你已读过 u8-l1（词典系统总览）和 u8-l3（Table 多级索引）。

### 2.1 构建期 vs 运行期是两套代码

librime 的词典代码分两段：

- **构建期**（本讲主角）：把人写的 `*.dict.yaml`（文本）编译成 `.prism.bin` / `.table.bin` / `.reverse.bin`（二进制内存映像）。这部分代码在「部署（deploy）」时跑一次。
- **运行期**（u8-l2 Prism、u8-l3 Table、u8-l5 Dictionary 查询）：用 `mmap` 把 `.bin` 直接挂载进内存，做音节切分与词条查询，跑在每个按键上。

`DictCompiler` 只属于构建期。它产出的 `.bin` 文件格式由 `MappedFile` + `OffsetPtr` 序列化地基（见 u8-l1）定义。

### 2.2 为什么词条权重要存成对数

`.dict.yaml` 里一条词条通常带一个权重（词频）。`DictCompiler` 在写盘前会把权重转成自然对数：

\[ w' = \ln(\max(w,\ \varepsilon)) \]

其中 \(\varepsilon\) 是 `DBL_EPSILON`，用来防止 \(\ln 0\)。原因是运行期排序要在「音节可信度 × 词条权重」的空间里做累乘（见 u7-l1 的对数可信度体系），概率相乘在对数空间退化为相加：\(\ln(a \cdot b) = \ln a + \ln b\)。所以构建期就把权重搬进对数空间，运行期只做加法。

### 2.3 校验和（checksum）= 文件的指纹

`ChecksumComputer`（基于 Boost 的 CRC-32）逐文件读取字节流，产出一个 32 位整数作为「这个文件内容的指纹」。两个文件内容相同 → 指纹相同；改动一个字节 → 指纹变。`DictCompiler` 用它来回答「`.dict.yaml` 自上次构建后改过没有」——只比较整数，不比较整个文件。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/rime/dict/dict_compiler.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h) | `DictCompiler` 类声明，定义 `Options`（`kRebuild`/`kDump` 等）与四个私有 `Build*` 方法。 |
| [src/rime/dict/dict_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc) | 本讲主角：`Compile()` 编排、`BuildTable`/`BuildPrism`/`BuildReverseDb` 三种产物写盘、checksum 决策。 |
| [src/rime/dict/entry_collector.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.h) | `RawDictEntry` 结构、`EntryCollector` 类（持有 `syllabary`/`entries`/`stems`）。 |
| [src/rime/dict/entry_collector.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc) | 三遍收集的真正实现：逐行解析 `.dict.yaml`、`CreateEntry`、`Finish`。 |
| [src/rime/dict/vocabulary.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h) | `Syllabary`/`Code`/`ShortDictEntry`/`VocabularyPage`/`Vocabulary` 数据模型。 |
| [src/rime/dict/vocabulary.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.cc) | `Vocabulary::LocateEntries` 多级定位、`SortHomophones` 同码词条排序。 |
| [src/rime/dict/dict_settings.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc) | 只读 `.dict.yaml` 文件头，提供列映射 `GetColumnIndex` 与子表清单 `GetTables`。 |
| [src/rime/algo/utilities.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/utilities.h) | `ChecksumComputer` / `Checksum`。 |

## 4. 核心概念与源码讲解

### 4.1 DictCompiler 总览：编译器的职责与 Compile() 入口

#### 4.1.1 概念说明

`DictCompiler` 是词典构建期的「总调度」。它本身不做具体的解析或序列化，而是**编排**三件事：

1. 决定**要不要重建**（基于 checksum）。
2. 调 `EntryCollector` 把 `*.dict.yaml` 读成内存里的 `syllabary`（音节表）+ `entries`（原始词条）。
3. 把内存结构交给 `Table` / `Prism` / `ReverseDb` 各自的 `Build` 写盘。

它的构造函数接收一个已存在的 `Dictionary*`，从中拿到词典名 `dict_name_`、附加包 `packs_`、以及三个产物对象的句柄 `prism_` / `tables_`：

参见 [src/rime/dict/dict_compiler.cc:L27-L35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L27-L35)，这里还构造了两个 `ResourceResolver`：`source_resolver_` 负责**从源目录读** `.dict.yaml`，`target_resolver_`（staging）负责**把 `.bin` 写到 staging 目录**（见 u8-l1 提到的「源目录读、staging 写」）。

`Options` 是一组位掩码，定义在 [src/rime/dict/dict_compiler.h:L27-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.h#L27-L32)：

```cpp
enum Options {
  kRebuildPrism = 1,
  kRebuildTable = 2,
  kRebuild = kRebuildPrism | kRebuildTable,
  kDump = 4,
};
```

`kRebuild` 强制全量重建，`kDump` 会把中间产物（文本形式）dump 出来——这是后面代码实践要用到的观察手段。

#### 4.1.2 核心流程

`Compile(schema_file)` 的高层流程可以用下面伪代码概括（省略 pack 循环）：

```
Compile(schema_file):
  1. 读 dict_name + ".dict.yaml" 的文件头 -> DictSettings
  2. 从 settings 取出待收集的子表清单 -> dict_files[]
  3. 计算 dict_files 的 checksum -> dict_file_checksum
     计算 schema_file 的 checksum -> schema_file_checksum
  4. 决策：对比现有 .table.bin / .prism.bin / .reverse.bin 的 checksum
     -> rebuild_table / rebuild_prism 两个布尔
  5. if rebuild_table:
        EntryCollector.Collect(dict_files)   # 三遍收集
        BuildTable(0, collector, ...)         # 写主 .table.bin + .reverse.bin
  6. if rebuild_prism:
        BuildPrism(schema_file, ...)          # 应用拼写代数，写 .prism.bin
  7. 对每个 pack（附加表）重复 BuildTable
  return true
```

四步构建的产物对应关系（承接 u8-l1）：

| 步骤 | 方法 | 产物 | 输入 |
|------|------|------|------|
| 1 | `BuildTable` | `*.table.bin` | `syllabary` + `Vocabulary` |
| 2 | `BuildReverseDb`（嵌在 `BuildTable` 里，仅主表） | `*.reverse.bin` | `syllabary` + `Vocabulary` + `stems` |
| 3 | `BuildPrism` | `*.prism.bin` | `syllabary` + 经拼写代数变换的 `Script` |
| 4 | pack 循环 | 附加 `*.table.bin` | 复用主表 `syllabary` |

#### 4.1.3 源码精读

整个 `Compile` 见 [src/rime/dict/dict_compiler.cc:L80-L208](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L80-L208)。其中文件头读取与子表清单解析在开头几步：

```cpp
auto dict_file = source_resolver_->ResolvePath(dict_name_ + ".dict.yaml");
// ... load_dict_settings_from_file ...
vector<path> dict_files;
get_dict_files_from_settings(&dict_files, settings, source_resolver_.get());
```

`get_dict_files_from_settings` 见 [src/rime/dict/dict_compiler.cc:L47-L62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L47-L62)：它读 `DictSettings::GetTables()` 返回的清单，把每个名字拼成 `<name>.dict.yaml` 路径，文件不存在就报错返回 `false`。

`GetTables()` 的逻辑在 [src/rime/dict/dict_settings.cc:L79-L96](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L79-L96)：默认清单 = 词典自身（`name`）+ `import_tables` 列出的子表，但禁止自引用（`table == dict_name()` 时跳过）。

进入主体后，先算 checksum，再决定是否重建（这两段留到 4.4 详讲），然后是核心的「收集 + 写盘」：

```cpp
if (rebuild_table) {
  EntryCollector collector;
  if (!BuildTable(0, collector, &settings, dict_files, dict_file_checksum))
    return false;
  syllabary = std::move(collector.syllabary);
}
// ...
if (rebuild_prism &&
    !BuildPrism(schema_file, dict_file_checksum, schema_file_checksum))
  return false;
```

注意一个关键衔接：**prism 的输入 syllabary 来自主 table**，而主 table 可能并未重建。见 [src/rime/dict/dict_compiler.cc:L145-L161](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L145-L161)：若 `rebuild_table` 为假但存在 pack，则从已有主表 `primary_table->GetSyllabary(&syllabary)` 取回音节表。这保证「只改拼写代数、只重建 prism」的场景下 prism 仍能拿到完整音节表。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在脑中跑通 `Compile` 的骨架，分清「决策」与「写盘」两段。

**步骤**：

1. 打开 [src/rime/dict/dict_compiler.cc:L80-L208](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L80-L208)。
2. 用笔把这段代码划成三段：①L84–L100（读文件头、算 checksum）；②L101–L144（rebuild 决策）；③L145–L205（真正的收集与写盘）。
3. 找到 pack 循环 [src/rime/dict/dict_compiler.cc:L162-L205](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L162-L205)，注意它用 `EntryCollector(std::move(syllabary))` 构造——这是「固定音节表」构造，含义见 4.2.1。

**需要观察的现象**：`Compile` 内部**没有出现任何「解析一行 YAML」的细节**，也**没有出现任何「写字节」的细节**——前者全在 `EntryCollector`，后者全在 `Table::Build`/`Prism::Build`/`ReverseDb::Build`。这正是「总调度」的体现。

**预期结果**：你能用一句话回答「`Compile` 自己做了什么」——它做了文件定位、checksum 比较、调用顺序编排；具体苦力活都委托出去了。

#### 4.1.5 小练习与答案

**练习 1**：`DictCompiler` 构造时拿到的 `prism_` / `tables_` 来自哪里？为什么要从外部传入而不是自己 new？

**答案**：来自构造参数 `Dictionary*`（`dictionary->prism()`、`dictionary->tables()`）。因为编译前后用的是**同一个** `Dictionary` 对象：编译前它持有旧 `.bin` 的句柄，编译后 `BuildTable`/`BuildPrism` 会用 `New<Table>(target_path)` 等重新赋值这些句柄（见 4.5.3），使运行期的 `Dictionary` 立即指向新产物，无需重建 `Dictionary`。

**练习 2**：`kDump` 选项打开后，会 dump 出哪些文件？

**答案**：两类。①`EntryCollector::Dump` 在 `BuildTable` 里被调用（[src/rime/dict/dict_compiler.cc:L229-L233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L229-L233)），把 `syllabary` 和所有 `RawDictEntry` 写成 `.txt`；②`Script::Dump` 在 `BuildPrism` 里被调用（[src/rime/dict/dict_compiler.cc:L351-L355](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L351-L355)），把拼写代数产出的 `Script` dump 成 `.txt`。

---

### 4.2 EntryCollector：三遍收集 .dict.yaml

#### 4.2.1 概念说明

`EntryCollector` 负责把文本形式的 `.dict.yaml` 词条读成内存结构。它的产出有两个：

- **`syllabary`**：`set<string>`，本词典出现过的**所有音节**（拼写串）的集合，如 `{"a", "ai", "an", "ang", "b", "ba", "ban", ...}`。因为 `set` 按字典序排列，音节会被赋予一个**按字典序递增**的 `SyllableId`。
- **`entries`**：`vector<of<RawDictEntry>>`，每个 `RawDictEntry` 是一条尚未编号的原始词条。

`RawDictEntry` 定义在 [src/rime/dict/entry_collector.h:L18-L22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.h#L18-L22)：

```cpp
struct RawDictEntry {
  RawCode raw_code;   // 拼写串数组，如 ["ni", "hao"]
  string text;        // 词条文字，如 "你好"
  double weight;      // 权重（原始值，尚未取对数）
};
```

其中 `RawCode` 是 `vector<string>`，`FromString` 按**单个空格**切分（[src/rime/algo/encoder.cc:L22-L25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L22-L25)），所以 `"ni hao"` → `["ni", "hao"]`。

`EntryCollector` 有两种构造方式（[src/rime/dict/entry_collector.h:L45-L46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.h#L45-L46)、[src/rime/dict/entry_collector.cc:L18-L21](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L18-L21)）：

- 默认构造：`build_syllabary = true`，**主表**用，会从词条里学习新音节。
- `EntryCollector(Syllabary&& fixed)`：`build_syllabary = false`，**附加包（pack）**用，音节表固定为父词典的，遇到未知音节就丢弃该条目。

#### 4.2.2 核心流程

「三遍收集」这个名字来自 `Finish()` 里的三条 `LOG(INFO) << "Pass N..."`。整体流程：

```
Collect(dict_files):           # 对每个子表文件
    Collect(dict_file):        # Pass 1：逐行解析
        读文件头 -> DictSettings（取列定义）
        for 每一行:
            按 \t 切成 row[]
            取 word = row[text_column]
            取 code_str = row[code_column]
            if code_str 非空: CreateEntry(word, code_str, weight)   # 直接入条目
            else:             encode_queue.push({word, weight})      # 留待 Pass 2 编码
    Finish():
        Pass 2: 清空 encode_queue，对每个无码词调 encoder->EncodePhrase 生成编码
        Pass 3: 若启用 preset_vocabulary（essay），合入预设词条表里尚未收录的词
```

列定义默认是 `text=0, code=1, weight=2`（[src/rime/dict/dict_settings.cc:L98-L117](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_settings.cc#L98-L117)），但 `.dict.yaml` 文件头可用 `columns:` 显式覆盖。

#### 4.2.3 源码精读

**Pass 1** 在 [src/rime/dict/entry_collector.cc:L57-L132](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L57-L132)。核心是逐行处理：

```cpp
auto row = strings::split(line, "\t");
// ...
const auto& word(row[text_column]);
string code_str, weight_str, stem_str;
if (code_column != -1 && num_columns > code_column && !row[code_column].empty())
  code_str = row[code_column];
// ...
collection.insert(word);
if (!code_str.empty()) {
  CreateEntry(word, code_str, weight_str);   // 有编码：直接建条目
} else {
  encode_queue.push({word, weight_str});     // 无编码：留给 Pass 2
}
```

注释里还藏一个细节：`# no comment` 这行会**关闭后续的注释识别**（[src/rime/dict/entry_collector.cc:L86-L91](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L86-L91)），这是为了支持词条文字本身以 `#` 开头的边界情况。

**`CreateEntry`** 在 [src/rime/dict/entry_collector.cc:L161-L221](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L161-L221)，做四件事：

1. `raw_code.FromString(code_str)` 把 `"ni hao"` 切成 `["ni","hao"]`。
2. 处理权重（见下方权重三态）。
3. **学习音节**：把 `raw_code` 里每个拼写串塞进 `syllabary`（若 `build_syllabary` 为假且音节不在固定表里，则丢弃整条）：

```cpp
for (const string& s : e->raw_code) {
  if (syllabary.find(s) == syllabary.end()) {
    if (build_syllabary) syllabary.insert(s);
    else { LOG(ERROR) << "dropping entry ..."; return; }
  }
}
```

4. 登记到 `entries` 并 `++num_entries`。

**权重三态**值得单独留意（[src/rime/dict/entry_collector.cc:L167-L192](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L167-L192)）：

| `weight_str` 形态 | 处理 |
|---|---|
| 空（如 `你好	ni hao`） | 若启用 preset_vocabulary，从 essay 取该词的预设权重 |
| 以 `%` 结尾（如 `80%`） | 先取 essay 权重，再乘百分比（相对权重） |
| 纯数字（如 `100`） | 直接当绝对权重 |

**Pass 2 / Pass 3** 在 [src/rime/dict/entry_collector.cc:L134-L159](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L134-L159)。Pass 2 用 `encoder`（`ScriptEncoder` 或 `TableEncoder`，由 `Configure` 决定）给无码词生成编码；Pass 3 把 essay 里尚未被 `collection` 收录的词也编码进来。最后清空 `collection`/`words`/`total_weight` 释放内存。

#### 4.2.4 代码实践（配置观察型）

**目标**：理解 `EntryCollector` 对同一行 `.dict.yaml` 的解析结果。

**步骤**：

1. 打开 [data/minimal/luna_pinyin.dict.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.dict.yaml) 的文件头（前 40 行）。注意 `use_preset_vocabulary: true` 且没有 `columns:` 定义，故走默认列 `text=0, code=1, weight=2`。
2. 找一行形如 `你好	ni hao`（无第三列 weight）。按 4.2.3 的逻辑推断：`code_str = "ni hao"`、`weight_str` 为空 → 走 `CreateEntry` → 权重从 essay 取。
3. 对比找一行带权重的，如 `你	ni	100`，推断它走「绝对权重」分支。

**需要观察的现象**：因为没有 `columns:`，所以即使某行只有两列（`你好	ni hao`），`weight_column` 仍是 2，`num_columns(2) > weight_column(2)` 为假，`weight_str` 保持空——这正是「无权重时回退到 essay」的触发条件。

**预期结果**：你能解释为什么 luna_pinyin 的词条大多不写权重却仍能按词频排序——权重来自 essay 预设词表。**待本地验证**：若想亲眼看到，可在 `CreateEntry` 入口加一行 `LOG(INFO) << "entry " << word << " weight=" << e->weight;`，重新部署后查看日志。

#### 4.2.5 小练习与答案

**练习 1**：为什么主表用默认构造的 `EntryCollector`，而 pack 用 `EntryCollector(std::move(syllabary))`？

**答案**：主表是音节的「源头」，要边读词条边学习新音节（`build_syllabary=true`）。pack 是附加表，必须复用主表已编号的音节集合，不能引入新音节（否则 `syllable_id` 编号体系会冲突），所以固定音节表（`build_syllabary=false`），遇到未知音节直接丢弃该条目并记 ERROR。

**练习 2**：一行 `.dict.yaml` 写成 `XX`（只有一列、无制表符），会发生什么？

**答案**：`row = ["XX"]`，`num_columns = 1`。`text_column = 0`，`row[text_column] = "XX"` 非空，所以 `word = "XX"`；但 `code_column = 1`，`num_columns(1) > code_column(1)` 为假，`code_str` 为空 → 进 `encode_queue`，留给 Pass 2 的 encoder 去生成编码。即「无码词」会走编码器路径而非直接丢弃。

---

### 4.3 Vocabulary：map<int, VocabularyPage> 树形组织

#### 4.3.1 概念说明

`EntryCollector` 产出的是**扁平**的 `entries`（一堆 `RawDictEntry`）。但 `Table` 需要的是**按音节序列索引**的结构，方便运行期按 `syllable_id` 序列逐级下钻（见 u8-l3）。`Vocabulary` 就是这两者之间的**中间表示**：一棵以音节 id 为键的多级树。

关键类型定义在 [src/rime/dict/vocabulary.h:L95-L104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L95-L104)：

```cpp
struct VocabularyPage {
  ShortDictEntryList entries;   // 落在本页的词条
  an<Vocabulary> next_level;    // 下一级子树（可为空）
};

class Vocabulary : public map<int, VocabularyPage> {
 public:
  ShortDictEntryList* LocateEntries(const Code& code);
  void SortHomophones();
};
```

`Vocabulary` 本身是 `map<int, VocabularyPage>`——键是 `syllable_id`（`int`），值是一个 `VocabularyPage`。每个 page 又挂一个 `next_level`（另一棵 `Vocabulary`），从而形成**深度等于词条音节数**的树。

这棵树的深度被一个常量截断：`Code::kIndexCodeMaxLength == 3`（[src/rime/dict/vocabulary.h:L21-L35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L21-L35)）。这与 u8-l3 讲的 Table 三级索引（HeadIndex / TrunkIndex / TailIndex）完全对应：

- 第 1 音节 → `Vocabulary` 的顶层 key（对应 HeadIndex）
- 第 2、3 音节 → `next_level` 的 key（对应 TrunkIndex）
- 第 4 音节及以后 → 全部塞进第 3 层 page 的 `entries`，靠 `ShortDictEntry::code` 携带完整序列（对应 TailIndex）

#### 4.3.2 核心流程

把一条 `Code`（`syllable_id` 序列）放进 `Vocabulary` 的过程由 `LocateEntries` 完成，它**边走边建**子树，返回最终落点的 `entries` 列表指针：

```
LocateEntries(code):           # code = [id0, id1, id2, ...]
  v = this
  for i in 0 .. code.size()-1:
    key = (i < 3) ? code[i] : -1      # 超过 3 个音节后，余下都用 key=-1
    page = (*v)[key]                  # map[] 不存在则插入空 page
    if i == last  或  i == kIndexCodeMaxLength:
      return &page.entries            # 到达落点
    else:
      if page.next_level 为空: page.next_level = new Vocabulary
      v = page.next_level             # 下钻
```

落点确定后，调用方把构造好的 `ShortDictEntry` push 进去。注意 `code[i]` 在第 4 音节以后全部塌缩到 key `-1`，这就是「TailIndex 把所有长词收口进同一个扁平数组」的来源。

#### 4.3.3 源码精读

`LocateEntries` 实现在 [src/rime/dict/vocabulary.cc:L123-L141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.cc#L123-L141)：

```cpp
ShortDictEntryList* Vocabulary::LocateEntries(const Code& code) {
  Vocabulary* v = this;
  size_t n = code.size();
  for (size_t i = 0; i < n; ++i) {
    int key = -1;
    if (i < Code::kIndexCodeMaxLength) key = code[i];
    auto& page((*v)[key]);                       // map[] 自动建空 page
    if (i == n - 1 || i == Code::kIndexCodeMaxLength) {
      return &page.entries;                       // 落点
    } else {
      if (!page.next_level) page.next_level = New<Vocabulary>();
      v = page.next_level.get();                  // 下钻
    }
  }
  return NULL;
}
```

注意 `(*v)[key]` 是 `map::operator[]`——**键不存在时自动插入一个默认构造的 `VocabularyPage`**，所以这棵树是懒构造的：只有被某条 `Code` 实际经过的路径才会被建出来。

`Code::kIndexCodeMaxLength` 同时被 `Code::CreateIndex` 用来截断「索引码」。见 [src/rime/dict/vocabulary.cc:L35-L44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.cc#L35-L44)：`CreateIndex` 只取前 3 个音节作为索引前缀，与 `LocateEntries` 的深度限制严格一致。

落点页内的词条用 `ShortDictEntry` 表示（[src/rime/dict/vocabulary.h:L37-L44](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L37-L44)），它的 `code` 字段保存**完整**音节序列（哪怕超过 3 个），`weight` 已是**对数权重**。同码词条（同音词）的排序由 `ShortDictEntry::operator<` 定义（[src/rime/dict/vocabulary.cc:L60-L66](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.cc#L60-L66)）：**按权重降序**，权重相同则保持原序（注释里写 `reduce carbon emission`，即不再做文本比较以省 CPU）。`SortHomophones`（[src/rime/dict/vocabulary.cc:L143-L150](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.cc#L143-L150)）递归地给每一页排序。

`Vocabulary` 在 `BuildTable` 里被构造并填充，核心循环见 [src/rime/dict/dict_compiler.cc:L234-L264](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L234-L264)：

```cpp
Vocabulary vocabulary;
map<string, SyllableId> syllable_to_id;
SyllableId syllable_id = 0;
for (const auto& s : collector.syllabary)          // set<string> 按字典序
  syllable_to_id[s] = syllable_id++;               // 故 id 按字典序递增
for (const auto& r : collector.entries) {
  Code code;
  for (const auto& s : r->raw_code)
    code.push_back(syllable_to_id[s]);             // 拼写串 -> id 序列
  RawCode().swap(r->raw_code);                      // 及时释放内存
  auto ls = vocabulary.LocateEntries(code);         // 定位落点页
  auto e = New<ShortDictEntry>();
  e->code.swap(code);
  e->text.swap(r->text);
  e->weight = log(r->weight > 0 ? r->weight : DBL_EPSILON);  // 取对数
  ls->push_back(e);
}
```

这段代码把 `EntryCollector` 的扁平产出「编织」成 `Vocabulary` 树。两个 `swap`（`r->raw_code` 和 `r->text`）是「指针级搬运」，把 `RawDictEntry` 的内容**零拷贝**转移到 `ShortDictEntry`，随后用 `vector<of<RawDictEntry>>().swap(collector.entries)` 整体释放原始数组——注释明确写「release memory in time to reduce memory usage」，因为大词典（如 luna_pinyin 数万条）的内存峰值是实打实的工程问题。

#### 4.3.4 代码实践（源码阅读型）

**目标**：跟踪一个具体词条在 `Vocabulary` 树里的落点。

**步骤**：以 `你好	ni hao` 为例（假设 `hao` 的 id = 5，`ni` 的 id = 20——因为 `set<string>` 按字典序，`h` < `n`）。

1. `raw_code = ["ni", "hao"]` → `code = [syllable_to_id["ni"], syllable_to_id["hao"]]` = `[20, 5]`。

   > 注意：`code` 的顺序是**输入顺序**（先 ni 后 hao），与 `syllabary` 的字典序无关。字典序只影响**每个音节的 id 取值**，不影响 `code` 数组里谁在前。

2. `LocateEntries([20, 5])`：
   - `i=0`：`key = code[0] = 20`，`page = (*this)[20]`。`i(0) != n-1(1)` 且 `i(0) != kIndexCodeMaxLength(3)` → 建 `next_level`，下钻。
   - `i=1`：`key = code[1] = 5`，`page = (*next_level)[5]`。`i(1) == n-1(1)` → 返回 `&page.entries`。
3. 落点 = 顶层 key=20 的 page → 其 `next_level` 里 key=5 的 page → `entries`。
4. `ShortDictEntry{ text:"你好", code:[20,5], weight: log(essay_weight) }` 被 push 进去。

**需要观察的现象**：双音节词只用到 `Vocabulary` 的两层（顶层 + 一层 `next_level`），叶子页的 `entries` 里收集所有 `ni hao` 同码的词（即「你好」「拟好」等同音词），随后由 `SortHomophones` 按对数权重降序排好。

**预期结果**：你能画出 `你好` 在 `Vocabulary` 树里的完整路径：`root → [ni的id] → next_level → [hao的id] → entries`。

#### 4.3.5 小练习与答案

**练习 1**：一个 5 音节的词条（如某成语 `a b c d e`），在 `Vocabulary` 树里落到哪一层？`ShortDictEntry::code` 有几个元素？

**答案**：落到第 3 层（`i == kIndexCodeMaxLength` 时返回），即顶层 key=`a的id` → next_level key=`b的id` → next_level key=`c的id` 的 page 的 `entries`。第 4、5 音节 `d`、`e` 不再下钻（`i >= 3` 时 `key = -1`，但因为 `i == kIndexCodeMaxLength` 已提前 return，实际上 `d`/`e` 根本没进循环的 key 计算）。`ShortDictEntry::code` 仍是**完整的 5 个元素** `[a,b,c,d,e]`，运行期 Table 查询时用 `extra_code` 携带后两个（对应 u8-l3 的 TailIndex）。

**练习 2**：为什么 `Vocabulary` 用 `map<int, VocabularyPage>` 而不是 `unordered_map`？

**答案**：`map` 按 key（`syllable_id`）**有序**。Table 构建时要把它转成有序的序列化索引（TrunkIndex 用二分查找，要求有序），`map` 的中序遍历天然给出升序 `syllable_id`，省去额外排序。`unordered_map` 虽然查找更快，但无序，反而要再排一次。

---

### 4.4 校验和（checksum）驱动的增量重建

#### 4.4.1 概念说明

`DictCompiler` 最具工程价值的设计是**增量重建**：部署时不无脑全量重编，而是只重建「真正变了」的部分。判变的依据是**校验和**。

每个 `.bin` 产物在 `Metadata` 里都存了构建时用的 `dict_file_checksum`（Prism 还多存一个 `schema_file_checksum`）。重建时，`DictCompiler` 重新算一遍源文件的 checksum，跟 `.bin` 里存的旧值比：

- 相等 → 文件没变，**复用**旧 `.bin`。
- 不等 → 文件变了，**重建**。

这套机制把「改了什么」精确映射到「重建什么」：

| 改动 | 触发重建 |
|------|----------|
| 改词条 / 加词条 | `dict_file_checksum` 变 → 重建 table（连带 reverse.bin，prism 复用音节表但也要重建） |
| 改 `speller/algebra`（拼写代数） | `schema_file_checksum` 变 → 只重建 prism |
| 改方案其它部分 | 都不重建词典 |

#### 4.4.2 核心流程

checksum 的计算与比较分散在 `Compile` 前半段：

```
compute_dict_file_checksum(0, dict_files, settings):
    cc = ChecksumComputer(0)
    for f in dict_files: cc.ProcessFile(f)
    if 启用 preset_vocabulary: cc.ProcessFile(essay 路径)   # essay 也算入指纹
    return cc.Checksum()

# 决策
rebuild_table = 现有 table 不存在        # 或 dict_file_checksum 不匹配
rebuild_prism  = 现有 prism 不存在        # 或 dict/schema checksum 任一不匹配
# 额外：reverse.bin 不匹配也强制 rebuild_table
```

一个容易忽略的细节：**essay（预设词表）也被算进 `dict_file_checksum`**。所以更新 essay 也会触发 table 重建——这合理，因为词条权重来自 essay。

#### 4.4.3 源码精读

checksum 计算函数 `compute_dict_file_checksum` 在 [src/rime/dict/dict_compiler.cc:L64-L78](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L64-L78)：

```cpp
ChecksumComputer cc(initial_checksum);
for (const auto& file_path : dict_files) {
  cc.ProcessFile(file_path);
}
if (settings.use_preset_vocabulary()) {
  cc.ProcessFile(PresetVocabulary::DictFilePath(settings.vocabulary()));
}
return cc.Checksum();
```

`ChecksumComputer` 本身基于 Boost CRC-32，定义在 [src/rime/algo/utilities.h:L18-L27](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/utilities.h#L18-L27)，每次 `ProcessFile` 把文件字节流喂进 CRC 累加器，`Checksum()` 取最终值。它支持 `initial_checksum` 参数，所以 pack 的 checksum 可以**接着主表的值继续累加**（见 pack 循环里 `compute_dict_file_checksum(dict_file_checksum, ...)`，[src/rime/dict/dict_compiler.cc:L187-L188](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L187-L188)）——即 pack 的指纹 = 主表指纹 ⊕ pack 文件指纹，这样主表变了 pack 也会跟着判变。

**table 的决策**在 [src/rime/dict/dict_compiler.cc:L101-L118](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L101-L118)：

```cpp
if (primary_table->Exists() && primary_table->Load()) {
  if (build_table_from_source) {
    rebuild_table = primary_table->dict_file_checksum() != dict_file_checksum;
  } else {
    dict_file_checksum = primary_table->dict_file_checksum();  // 无源文件：沿用旧值
  }
  primary_table->Close();
} else if (build_table_from_source) {
  rebuild_table = true;                        // .bin 不存在：必须建
} else {
  LOG(ERROR) << "neither ... .dict.yaml nor ... .table.bin exists.";
  return false;
}
```

注意第二个分支：如果源 `.dict.yaml` 不存在（`build_table_from_source = false`）但旧 `.table.bin` 在，就把 `.bin` 里存的 checksum 读出来当作当前 checksum，并**不重建**——这支持「只发预编译 `.bin`、不发源文件」的分发方式（见 u8-l1 提到的 prebuilt）。

**prism 的决策**在 [src/rime/dict/dict_compiler.cc:L119-L125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L119-L125)，多比一个 `schema_file_checksum`：

```cpp
if (prism_->Exists() && prism_->Load()) {
  rebuild_prism = prism_->dict_file_checksum() != dict_file_checksum ||
                  prism_->schema_file_checksum() != schema_file_checksum;
  prism_->Close();
} else {
  rebuild_prism = true;
}
```

这就是「改拼写代数只重建 prism」的根因：拼写代数写在 schema 里，schema 变只动 `schema_file_checksum`，table 不受影响。

**reverse.bin 的反向强制**在 [src/rime/dict/dict_compiler.cc:L129-L138](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L129-L138)：若 `reverse.bin` 不存在或 checksum 不匹配，会把 `rebuild_table` 强制置真——因为 `reverse.bin` 是 table 构建的副产物（`BuildReverseDb` 嵌在 `BuildTable` 里），不能脱离 table 单独重建。

最后两条是选项覆盖（[src/rime/dict/dict_compiler.cc:L139-L144](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L139-L144)）：`kRebuildTable`/`kRebuildPrism` 直接强制对应重建，绕过 checksum 比较。

#### 4.4.4 代码实践（源码阅读型）

**目标**：验证「改拼写代数只重建 prism」这条结论。

**步骤**：

1. 想象一个已部署好的 luna_pinyin，`.table.bin` / `.prism.bin` 都已存在且 checksum 匹配。
2. 在 `luna_pinyin.schema.yaml` 的 `speller/algebra` 末尾加一条无害规则（如 `derive/^/x/` 之类的派生，仅用于改变 schema 文件内容）。
3. 跟踪 `Compile`：
   - `dict_file_checksum`：`.dict.yaml` 没改 → 与 table 存的值相等 → `rebuild_table = false`。
   - `schema_file_checksum`：schema 改了 → 与 prism 存的值不等 → `rebuild_prism = true`。
4. 于是只跑 `BuildPrism`，跳过 `BuildTable`。

**需要观察的现象**：部署日志里会看到 `building prism...` 但**不会**看到 `building table:`。如果开了 `kDump`，只会新生成 prism 的 `.txt` dump。

**预期结果**：你确认了 checksum 机制把「内容变化」精确路由到「产物重建」。**待本地验证**：实际跑一次部署（`rime_deployer --compile` 或前端触发的部署）观察日志中的 `building` 行。

#### 4.4.5 小练习与答案

**练习 1**：用户只改了 `*.dict.yaml` 里某个词条的权重（如 `你	ni	100` 改成 `你	ni	200`），会触发哪些重建？

**答案**：`dict_file_checksum` 变 → `rebuild_table = true`、`rebuild_prism = true`（prism 也比 `dict_file_checksum`）。所以 table、reverse.bin、prism **全部重建**。因为权重进 table，而 prism 的 checksum 也绑定 dict 文件。

**练习 2**：为什么 pack 的 checksum 要用 `compute_dict_file_checksum(dict_file_checksum, ...)`（带上主表 checksum 作为初值），而不是从 0 开始算？

**答案**：让 pack 的指纹**依赖于主表指纹**。主表变了（`dict_file_checksum` 变），即使 pack 文件本身没改，pack 的「复合指纹」也会跟着变，从而触发 pack 重建——因为 pack 复用主表的 `syllabary` 编号，主表音节编号一变，pack 的编号必须重算。若从 0 算，主表变了 pack 不会判变，导致 pack 引用错误的 `syllable_id`。

---

### 4.5 三种产物写盘：Table / Prism / ReverseDb

#### 4.5.1 概念说明

决策完成后，三个 `Build*` 方法把内存结构序列化成 `.bin`。它们都遵循同一个模式：

1. 用 `target_resolver_` 把产物路径**重定位到 staging 目录**（`relocate_target`，[src/rime/dict/dict_compiler.cc:L210-L214](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L210-L214)）。
2. 用新路径 `New<Table/Prism/ReverseDb>(target_path)` 重建产物对象句柄。
3. 调 `Build(...)` 填充内存映像。
4. 调 `Save()` 落盘。

`relocate_target` 只取源路径的**文件名**，交给 `target_resolver_` 重新解析成 staging 目录下的路径——这就是「写到 staging 而非覆盖源目录」的实现，保证构建过程不会污染用户数据目录。

#### 4.5.2 核心流程

三个产物的输入与产物：

```
BuildTable(table_index, collector, settings, dict_files, checksum):
    target_path = staging/<name>.table.bin
    table = new Table(target_path)
    collector.Configure(settings); collector.Collect(dict_files)   # 三遍收集
    把 entries 编织进 Vocabulary（见 4.3）
    if sort_order != "original": vocabulary.SortHomophones()
    table.Build(syllabary, vocabulary, num_entries, checksum)      # 序列化多级索引
    table.Save()
    if table_index == 0:                                            # 仅主表
        BuildReverseDb(settings, collector, vocabulary, checksum)

BuildPrism(schema_file, dict_checksum, schema_checksum):
    target_path = staging/<name>.prism.bin
    prism = new Prism(target_path)
    从主 table 取 syllabary
    读 schema 的 speller/algebra -> Projection
    projection.Apply(&script)        # 拼写代数：派生模糊音/缩写拼写
    prism.Build(syllabary, &script, dict_checksum, schema_checksum)  # 建双数组 trie
    prism.Save()

BuildReverseDb(settings, collector, vocabulary, checksum):
    target_path = staging/<name>.reverse.bin
    reverse_db.Build(settings, syllabary, vocabulary, stems, checksum)  # 建 word->code 反查
    reverse_db.Save()
```

#### 4.5.3 源码精读

`BuildTable` 完整实现见 [src/rime/dict/dict_compiler.cc:L216-L278](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L216-L278)。其中 `Table::Build` 的签名在 [src/rime/dict/table.h:L198-L201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h#L198-L201)：

```cpp
bool Build(const Syllabary& syllabary,
           const Vocabulary& vocabulary,
           size_t num_entries,
           uint32_t dict_file_checksum = 0);
```

它接收 `Vocabulary` 树，按 u8-l3 讲的三级索引（Head/Trunk/Tail）序列化。`table->Remove()` 先删旧文件再 `Build`+`Save`，避免残留。注意 `BuildReverseDb` 嵌在 `BuildTable` 末尾且**只在 `table_index == 0`**（主表）时调用（[src/rime/dict/dict_compiler.cc:L272-L276](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L272-L276)）——所以只有主词典有 `.reverse.bin`，pack 没有。

`BuildPrism` 见 [src/rime/dict/dict_compiler.cc:L296-L367](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L296-L367)。它的关键是从 schema 读拼写代数并应用到 syllabary：

```cpp
Projection p;
auto algebra = config.GetList("speller/algebra");
if (algebra && p.Load(algebra)) {
  for (const auto& x : syllabary) script.AddSyllable(x);   // 原始音节
  if (!p.Apply(&script)) script.clear();                   # 派生模糊音/缩写
}
// ...
prism_->Build(syllabary, script.empty() ? nullptr : &script,
              dict_file_checksum, schema_file_checksum);
```

这段把 u7（拼写代数）和 u8-l2（Prism）缝起来：`Projection::Apply(&script)` 对每个原始音节施加 `xform/derive/fuzz/abbrev` 等运算（见 u7-l2），派生出模糊音、缩写等变体拼写，全部塞进 `Script`；Prism 再把这些**派生拼写**也建成 trie 键（这正是模糊音 `li` 能查到音节 `ni` 的根本原因，详见 u8-l2）。若 schema 没有 `speller/algebra` 或解析失败，`script` 为空，Prism 退化为只索引原始音节。

代码里还有一段被 `#if 0` 注释掉的「corrector（纠错索引）」构建逻辑（[src/rime/dict/dict_compiler.cc:L329-L349](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L329-L349)），目前未启用，可作为了解历史设计的线索。

`BuildReverseDb` 见 [src/rime/dict/dict_compiler.cc:L280-L294](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L280-L294)，它委托 `ReverseDb::Build`（签名 [src/rime/dict/reverse_lookup_dictionary.h:L47-L51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/reverse_lookup_dictionary.h#L47-L51)），用 `collector.stems`（`ReverseLookupTable` = `hash_map<string, set<string>>`，词→编码集合）构建反查库，供运行期「按字反查编码」使用。

#### 4.5.4 代码实践（运行观察型）

**目标**：用 `kDump` 选项亲眼看到构建的中间产物。

**步骤**：

1. 定位 `DictCompiler` 被调用的上层（部署任务 `WorkspaceUpdate` / `SchemaUpdate`，见 u9-l2），或在测试中找到设置 `set_options(kDump)` 的入口。
2. 构建一次主词典，查看 staging 目录下生成的文件：
   - `<name>.table.bin` —— 主码表
   - `<name>.reverse.bin` —— 反查库
   - `<name>.prism.bin` —— 音节索引
   - （因 `kDump`）`<name>.txt`（词条 dump）、`<name>.txt`（prism 的 Script dump）
3. 打开词条 dump，确认其格式来自 `EntryCollector::Dump`（[src/rime/dict/entry_collector.cc:L247-L259](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L247-L259)）：开头是 `# syllabary:` 列出所有音节，随后每行 `text\traw_code\tweight`。

**需要观察的现象**：dump 文件里 `# syllabary:` 段的音节**按字典序排列**（因为 `syllabary` 是 `set<string>`），印证了 4.3 所说的「`syllable_id` 按字典序递增」。

**预期结果**：你能把 dump 文件里某条 `你好	ni hao	<weight>` 与本讲跟踪的 `RawDictEntry` / `ShortDictEntry` 一一对应。**待本地验证**：staging 目录的具体位置取决于部署器配置（见 u9-l1 的 `staging` 目录）。

#### 4.5.5 小练习与答案

**练习 1**：`BuildPrism` 里为什么要先 `primary_table->Load()` + `GetSyllabary()`，而不是直接用 `collector.syllabary`？

**答案**：因为 prism 可能**在不重建 table 的情况下单独重建**（见 4.4：只改拼写代数时 `rebuild_table = false`）。此时 `Compile` 不会跑 `EntryCollector`，手里没有 `collector.syllabary`，只能从已存在的主 `.table.bin` 里把音节表读出来作为 prism 的输入。代码里对应 [src/rime/dict/dict_compiler.cc:L304-L309](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L304-L309)。

**练习 2**：为什么 `BuildReverseDb` 只对主表（`table_index == 0`）调用，pack 不建 reverse.bin？

**答案**：反查库（`.reverse.bin`）的用途是「按字反查编码」，服务于反查翻译器 `reverse_lookup_translator`（见 u6-l4）。这是一个**整个词典共用**的功能，主表的 reverse.bin 已经覆盖了主词典的全部词条；pack 是附加表，通常不需要独立反查，故省略以节省构建时间与磁盘空间。

---

## 5. 综合实践

**任务**：完整跟踪词条 `你好	ni hao` 从 `luna_pinyin.dict.yaml` 到 `.table.bin` 的旅程，并画一张数据流图。

请按以下顺序跟踪并记录每一步的**数据形态变化**：

1. **文本行**：在 [data/minimal/luna_pinyin.dict.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.dict.yaml) 中定位 `你好	ni hao` 这一行（无 weight 列）。
2. **Pass 1 解析**（[entry_collector.cc:L94-L121](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L94-L121)）：`row = ["你好", "ni hao"]` → `word="你好"`, `code_str="ni hao"`, `weight_str=""` → 走 `CreateEntry`。
3. **CreateEntry**（[entry_collector.cc:L161-L221](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/entry_collector.cc#L161-L221)）：
   - `raw_code.FromString("ni hao")` → `["ni","hao"]`（[encoder.cc:L22-L25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/encoder.cc#L22-L25)）
   - `weight_str` 空 + `use_preset_vocabulary: true` → 从 essay 取权重 `w`
   - 音节 `"ni"`、`"hao"` 插入 `syllabary`
   - `entries.push_back(RawDictEntry{raw_code:["ni","hao"], text:"你好", weight:w})`
4. **Vocabulary 编织**（[dict_compiler.cc:L234-L264](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L234-L264)）：
   - `syllable_to_id["ni"]=N`, `syllable_to_id["hao"]=H`（具体值由字典序决定，`H < N`）
   - `code = [N, H]`
   - `LocateEntries([N,H])` → 顶层 key=N 的 page 的 next_level 里 key=H 的 page 的 `entries`
   - `ShortDictEntry{ text:"你好", code:[N,H], weight: log(w) }` 入列
5. **排序**：`SortHomophones` 把该页内 `ni hao` 同码词条按 `log(weight)` 降序排。
6. **Table 写盘**（[dict_compiler.cc:L265-L270](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc#L265-L270)）：`table->Build(syllabary, vocabulary, num_entries, checksum)` 把这棵 Vocabulary 树序列化成 `.table.bin` 的三级索引（u8-l3），`table->Save()` 落盘到 staging。

**产出**：画一张数据流图，节点为「文本行 → row[] → RawDictEntry → Code+[id] → ShortDictEntry（Vocabulary 页内）→ .table.bin 的 HeadIndex/TrunkIndex 落点」，每条边上标注发生转换的代码位置（文件:行号）。

**进阶**：把 `你好	ni hao` 换成一个 4 音节词（如 `abcd	a b c d`），重画它在 `Vocabulary` 树里的落点（应落到第 3 层 key=`a的id` 的 page 的 `entries`，且 `ShortDictEntry::code` 含全部 4 个 id），验证 4.3.5 练习 1 的结论。

## 6. 本讲小结

- `DictCompiler::Compile` 是构建期总调度：做文件定位、checksum 决策、调用编排；具体的解析委托 `EntryCollector`，写盘委托 `Table`/`Prism`/`ReverseDb` 各自的 `Build`。
- `EntryCollector` 用「三遍收集」把 `.dict.yaml` 读成 `syllabary`（`set<string>`，按字典序决定 `syllable_id`）+ 扁平 `entries`（`RawDictEntry`）。Pass 1 逐行解析有码词条、把无码词入队；Pass 2 用 encoder 给无码词生成编码；Pass 3 合入 essay 预设词表。
- `Vocabulary` 是 `map<int, VocabularyPage>` 的多级树，深度受 `Code::kIndexCodeMaxLength == 3` 截断，与 Table 的 Head/Trunk/Tail 三级索引一一对应；`LocateEntries` 边走边懒建子树，落点页收集同码词条（同音词）。
- 词条权重在写盘前转成对数（`log(max(w, ε))`），与运行期的对数可信度体系对齐，使概率相乘退化为相加。
- 校验和（CRC-32）驱动增量重建：改词条 → 重建 table+prism+reverse；改拼写代数（schema）→ 只重建 prism；essay 也算入 dict 指纹；pack 指纹以主表指纹为初值，主表变则 pack 跟着判变。
- 三个产物写到 staging 目录（`relocate_target` 只取文件名重定位），`BuildReverseDb` 只对主表调用；Prism 在单独重建时从主 `.table.bin` 回读 syllabary。

## 7. 下一步学习建议

本讲讲清了「构建期」如何造出 `.bin`。接下来：

- **u8-l5（Dictionary 查询主链路）**：看运行期如何用本讲产出的 `.prism.bin` + `.table.bin` 回答一次翻译查询——`SyllableGraph → Prism 取 syllable_id → Table 沿多级索引查词条 → DictEntryCollector 按 end_pos 分组`。本讲的 `Code`、`ShortDictEntry`、`Vocabulary` 树在查询侧会以 `TableAccessor` / `DictEntry` 的形式再次出现。
- **u9-l1（Deployer 与 DeploymentTask）**：看 `DictCompiler` 是被谁、在什么线程、按什么任务顺序调用的——它实际跑在部署器的后台工作线程上，由 `SchemaUpdate` / `WorkspaceUpdate` 等任务驱动。
- **u9-l5（Encoder 编码生成）**：本讲的 Pass 2 调用的 `ScriptEncoder` / `TableEncoder` 来自这里，深入理解「无码词如何被规则编码（如形码的 `AaZa` 公式）」。

建议继续阅读 [src/rime/dict/dict_compiler.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dict_compiler.cc) 全文，并对照一次真实部署的日志（搜 `building table` / `building prism` / `Pass N`）印证本讲描述的流程。
