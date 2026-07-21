# Dictionary 查询主链路

## 1. 本讲目标

经过 u8-l1 ~ u8-l4，我们已经知道词典在**构建期**如何从 `*.dict.yaml` 被编译成 `.prism.bin`（音节索引）和 `.table.bin`（码表）。本讲要回答的是**运行期**最关键的一个问题：

> 用户敲下一段按键、被切成了音节图之后，引擎是怎么把「音节」翻译成「词条候选」的？

读完本讲，你应当能够：

- 说清 `Dictionary` 这个「门面」类由哪些部件组合而成、作为一个组件如何被装配出来。
- 画出 `Dictionary::Lookup(SyllableGraph)` 的完整数据流：音节图 → `Table::Query`（沿图做 BFS）→ `TableQueryResult` → `DictEntryCollector`（按 `end_pos` 分组）。
- 理解 `Chunk` 这个中间数据结构，以及 `DictEntryIterator` 如何用「拉模型 + 增量排序」按需产出 `DictEntry`。
- 掌握从字符串反查词条的 `LookupWords` 路径（经 Prism 反查 `syllable_id`），以及把 `syllable_id` 序列还原成可读拼写的 `Decode`。
- 看懂候选权重的对数空间计算方式。

本讲是 u6-l4「Translator 组件族」里 `script_translator` / `reverse_lookup_translator` 真正调用的底层接口，也是 u8-l6「用户词典」的姊妹篇（`UserDictionary` 沿用了几乎相同的 collector 模型）。

## 2. 前置知识

本讲默认你已经掌握以下内容（对应前置讲义）：

- **`SyllableGraph`（音节图）**（u7-l3）：输入串被 `Syllabifier` 切成所有可能的音节组合，结果存在三张表里——`vertices`（位置→最优拼写类型）、`edges`（`start→end→syllable→属性` 的三级邻接表）、`indices`（`edges` 的转置，按「起点位置 + 音节 id」查属性列表）。本讲的查询就是沿这张图走。
- **`Prism`（音节索引）**（u8-l2）：一张双数组 Trie，把「拼写串」映射到 `syllable_id`（中间还隔着一张 `SpellingMap`，承载模糊音/缩写的一对多关系）。
- **`Table`（码表）**（u8-l3）：一张固定三层的多级数组索引（`HeadIndex`/`TrunkIndex`/`TailIndex`），深度由 `Code::kIndexCodeMaxLength == 3` 钉死，把 `syllable_id` 序列映射到词条。超过 3 个音节的词条，前 3 个音节进索引、剩余音节收口进 `TailIndex` 的 `extra_code`。
- **`Code`**：本质是 `vector<SyllableId>`，即一个音节 id 序列（vocabulary.h:21-35）。

两个关键术语回顾：

- **`syllable_id`**：一个 32 位整数，是某个音节（如 `zhong`、`shu`）在 `Table` 内部 `Syllabary`（音节表）里的下标。它是 Prism 与 Table 之间沟通的「硬通货」——音节图里的边、码表里的索引键，用的都是它。
- **`end_pos`**：输入串里某个字节偏移位置。音节图的每条边都有一个 `[start, end)` 区间；查询结果按「这条路径消费输入消费到了第几个字节」来分组，这个字节位置就是 `end_pos`。

还要记住一个贯穿全讲的工程惯例：**权重与可信度都工作在自然对数空间**。这样「概率相乘」会退化为「对数相加」，便于沿音节图逐条边累加。具体到本讲，候选的最终权重是：

\[
\text{weight} = \text{e.weight} - \log(10^8) + \text{credibility}
\]

其中 \(\log(10^8) \approx 18.4207\)，是码表存储时的归一化常数；`credibility` 是拼写代数/音节图带来的对数可信度。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/rime/dict/dictionary.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.h) | `Dictionary` 门面类、`DictEntryIterator`、`DictEntryCollector` 别名、`DictionaryComponent` 的声明 |
| [src/rime/dict/dictionary.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc) | 本讲主角：`Lookup` / `lookup_table` / `LookupWords` / `Decode` 的实现，以及内部 `Chunk`、`QueryResult`、`match_extra_code` |
| [src/rime/dict/vocabulary.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h) | `SyllableId`/`Syllabary`/`Code`/`DictEntry` 等公共数据类型 |
| [src/rime/dict/table.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.h) | `TableQuery`、`TableAccessor`、`TableQueryResult` 与 `Table` 的查询接口声明 |
| [src/rime/dict/table.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc) | `Table::Query`（沿音节图 BFS）、`TableQuery::Walk/Advance/Access` 的实现 |
| [src/rime/algo/syllabifier.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/algo/syllabifier.h) | `SyllableGraph` 数据结构（查询的输入） |
| [src/rime/dict/prism.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/prism.h) | `Prism` 的查询接口（反查路径用） |
| [test/dictionary_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/dictionary_test.cc) | 端到端测试 `ScriptLookup` / `SimpleLookup` / `PredictiveLookup`，是本讲实践的依据 |

---

## 4. 核心概念与源码讲解

### 4.1 Dictionary 门面：Prism 与 Table 的组合

#### 4.1.1 概念说明

前面几讲我们分别认识了 `Prism`（音节索引）和 `Table`（码表）这两个独立的二进制产物。但运行期的 Translator 并不直接和它们打交道，而是通过一个**门面（Facade）**——`Dictionary`。

`Dictionary` 把「一个 Prism + 若干 Table」打包成一个整体，对外暴露三个核心查询方法：

| 方法 | 输入 | 输出 | 用途 |
|------|------|------|------|
| `Lookup` | `SyllableGraph`（音节图） | `DictEntryCollector`（按 `end_pos` 分组） | **正向查询**：音码输入法（拼音）的主力路径 |
| `LookupWords` | 字符串拼写 | `DictEntryIterator` | **反查**：形码输入法（仓颉）直接按编码查词、或反查翻译器 |
| `Decode` | `Code`（音节 id 序列） | `vector<string>`（拼写串列表） | 把内部 id 还原成人类可读拼写，做注释/反查展示 |

`Dictionary` 还是一个**组件**（继承 `Class<Dictionary, const Ticket&>`），名字 `"dictionary"`，由 `DictionaryComponent` 工厂生产。这意味着方案 YAML 里写的 `translator/dictionary: luna_pinyin`，最终就是由这个组件读取、定位到 `luna_pinyin.prism.bin` 与 `luna_pinyin.table.bin`。

#### 4.1.2 核心流程

`Dictionary` 的装配分两步：

1. **组件装配期**（`DictionaryComponent::Create`）：读方案配置，拿到 `dict_name` / `prism_name` / `packs`，用 `ResourceResolver` 解析成磁盘路径，创建（或从缓存复用）`Prism` 与 `Table` 对象，组装成 `Dictionary`。
2. **加载期**（`Load`）：实际 `mmap` 打开主 table 与 prism；`packs`（附加词典表）是可选的，逐个尝试打开。

一个值得注意的设计：`prism_` 用 `an<Prism>`（共享指针），`tables_` 是 `vector<of<Table>>`，且 `DictionaryComponent` 内部用 `map<string, weak<Prism>>` / `weak<Table>` 做缓存。也就是说**同名 Prism/Table 在一个进程里只被加载一次，多个 Dictionary 共享同一份 mmap 映射**——这是输入法里多个翻译器引用同一本词典时的重要省内存优化。

#### 4.1.3 源码精读

`Dictionary` 类的数据成员非常简洁，核心就是「名字 + 若干表 + 一个棱镜」：

[src/rime/dict/dictionary.h:62-104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.h#L62-L104) 定义了 `Dictionary` 类，其中 `tables_` 是主表 + 附加表（packs），`primary_table()` 就是 `tables_[0]`，`prism()` 返回唯一的音节索引。

装配逻辑在 `DictionaryComponent::Create`：

[src/rime/dict/dictionary.cc:425-451](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L425-L451) 从方案的 `ticket.name_space + "/dictionary"` 读词典名；`"/prism"` 读不到时**默认等于词典名**；`"/packs"` 读附加表列表（可空）。这与 u2-l3、u6-l4 讲过的方案配置完全对应。

[src/rime/dict/dictionary.cc:453-478](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L453-L478) 是真正的构造：先从 `table_map_[dict_name].lock()` 尝试复用旧的 `weak_ptr`，命中失败才 `ResolvePath` 并 `New<Table>`；Prism 同理。注意第 467 行 `tables = {std::move(primary_table)}`，主表永远是第 0 个。

加载逻辑：

[src/rime/dict/dictionary.cc:379-407](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L379-L407) `Load()` 先打开主 table（第 0 个），再打开 prism，最后逐个尝试 packs（第 396 行循环从 `i = 1` 开始）。`loaded()` 的判定是「至少一张表已开 + prism 已开」，所以**没有 prism 的纯形码方案会判定为未加载**——这呼应了 `LookupWords` 必须依赖 prism 的事实。

#### 4.1.4 代码实践

**实践目标**：理解方案配置如何驱动 `Dictionary` 的装配。

**操作步骤**：

1. 打开 [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml)，找到 `translator:` 段下的 `dictionary:` 字段，记下它的值（应为 `luna_pinyin`）。
2. 阅读 [dictionary.cc:425-451](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L425-L451)，对照确认：当方案没写 `prism:` 时，`prism_name` 会取什么值？
3. 思考：如果方案里同时配置了 `translator/dictionary: luna_pinyin` 和 `reverse_lookup/dictionary: luna_pinyin`（两个翻译器引用同一本词典），`DictionaryComponent` 的 `weak_ptr` 缓存会如何表现？

**预期结果**：`prism_name` 默认等于 `dict_name`（即 `luna_pinyin`）；两个翻译器会共享同一个 `Prism` 与 `Table` 对象（`weak_ptr` 命中），不会重复 mmap。

#### 4.1.5 小练习与答案

**练习 1**：`Dictionary` 为什么要同时持有 `Prism` 和 `Table` 两类对象，而不是只持有 `Table`？

**参考答案**：因为 `LookupWords`（字符串反查）需要先经 `Prism` 把字符串拼写翻译成 `syllable_id`，才能到 `Table` 里查词条；而 `Lookup(SyllableGraph)` 路径里，音节图本身已经携带 `syllable_id`（音节图是 `Syllabifier` 用 Prism 构建的），所以那条路径只用 `Table`。两类产物职责互补，缺一不可。

**练习 2**：`primary_table()` 与 `tables_` 的关系是什么？为什么要有 packs？

**参考答案**：`primary_table()` 恒为 `tables_[0]`，是主词典表；packs 是附加词典表（`tables_[1..]`），用于在不修改主词典的前提下追加词条（如扩展词库）。查询时主表与 packs 都会被查（见 4.2.3 的 `Lookup` 循环）。

---

### 4.2 正向查询主链路：Lookup → lookup_table → Table::Query

#### 4.2.1 概念说明

这是本讲的**主菜**。当用户敲拼音 `shurufa`，`Syllabifier` 先把它切成音节图（边：`shu`、`ru`、`fa` 等多种切法），然后 `script_translator` 调 `Dictionary::Lookup(syllable_graph, 0)`。

`Lookup` 要做的是：**沿着音节图的每一条合法路径，到码表里把对应的词条都捞出来，并按「这条路径结束在第几个字节」分组**。

分组结果是 `DictEntryCollector`：

```cpp
using DictEntryCollector = map<size_t, DictEntryIterator>;  // dictionary.h:54
```

`key` 是 `end_pos`（字节位置），`value` 是「结束在该位置的所有候选」组成的迭代器。为什么按 `end_pos` 分组？因为上层翻译器需要知道「消耗了前 3 个字节能得到哪些候选」「消耗了前 5 个字节呢？」——这样才能在「短词先上屏」与「长词整句」之间做组合（u6-l4 讲过的整句拼装）。

以输入 `shurufa`（共 7 字节）为例，`Lookup` 返回的 collector 大致长这样（来自本讲实践依据 `ScriptLookup` 测试）：

```
end_pos = 3  -> [输]                      // 1 音节词（code.size() == 1）
end_pos = 5  -> [输入, 舒乳, ...]          // 2 音节词（code.size() == 2）
end_pos = 7  -> [输入法]                   // 3 音节词（code.size() == 3）
```

#### 4.2.2 核心流程

整条链路可以拆成三层，逐层「翻译」数据结构：

```
SyllableGraph                       (音节图：位置 + syllable_id + 属性)
     │  Table::Query（沿图 BFS，逐层 Walk 码表索引）
     ▼
TableQueryResult                    (map<int, vector<TableAccessor>>)
     │  lookup_table（处理 extra_code 长词，包装成 Chunk）
     ▼
DictEntryCollector                  (map<size_t, DictEntryIterator>)
```

**第一层：`Table::Query`（码表层 BFS）。** 这是 u8-l3 讲过的三级索引（Head/Trunk/Tail）的运行期消费者。它用 BFS 遍历音节图：

- 队列里每个元素是 `(当前位置 current_pos, 一个 TableQuery 状态)`。
- `TableQuery` 是一个**有状态游标**，记录「当前已经下钻到码表第几层、累计 credibility/quality_len」。
- 在 `current_pos` 处，查 `syllable_graph.indices` 得到所有「从该位置出发的音节及其属性」。
- 对每个音节 `syll_id`：
  1. `query.Access(syll_id)` 在**当前层级**取词条（即「到此为止的词」，比如走到第 2 个音节取双字词），记入 `result[end_pos]`。
  2. `query.Advance(syll_id)` **下钻一层**，把新状态推入队列，继续往后匹配更长的词。
- 第 `kIndexCodeMaxLength`（=3）层封顶：到达第 3 层后用 `Access(-1)` 取 `TailIndex` 里的长词。

**第二层：`lookup_table`（适配层）。** `TableQueryResult` 是 `map<int, vector<TableAccessor>>`，但最终要的是 `DictEntryCollector`（按 `end_pos` 分组）。`lookup_table` 负责这个转换，并特别处理 `TailIndex` 的长词：长词的前 3 个音节已被索引、剩余音节存在 `extra_code` 里，需要 `match_extra_code` 沿音节图继续向后匹配，才能确定它真正的结束位置 `end_pos`。

**第三层：`Lookup`（门面层）。** 遍历每张表（主表 + packs）调用 `lookup_table`，把结果并入同一个 collector；最后对每个 `end_pos` 分组做 `Sort()`，并可选挂上黑名单过滤器。

#### 4.2.3 源码精读

先看门面 `Lookup`：

[src/rime/dict/dictionary.cc:271-297](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L271-L297) 核心是一个「对每张表查一次」的循环（第 279 行 `for (const auto& table : tables_)`），合并进同一个 `collector`；查空了直接返回 `nullptr`（第 285 行）；最后第 288 行循环对每个 `end_pos` 分组做 `Sort()` 与可选黑名单过滤。注意 `initial_credibility` 这个参数会被累加进每条候选的 credibility（脚本翻译器在查用户词典时用它注入用户词的可信度加成）。

真正的查询委托给文件内的静态函数 `lookup_table`：

[src/rime/dict/dictionary.cc:238-269](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L238-L269) 先调 `table->Query(syllable_graph, start_pos, &result)` 拿到 `TableQueryResult`，然后遍历它。关键分支在第 254 行：

- **没有 `extra_code`**（短词，≤3 音节）：直接 `(*collector)[end_pos].AddChunk({table, a, cr, q})`，结束位置就是 Table 报上来的 `end_pos`。
- **有 `extra_code`**（长词，>3 音节，来自 `TailIndex`）：`do { match_extra_code(...) } while (a.Next())` 逐条 LongEntry 地处理，用 `match_extra_code` 沿音节图把 `extra_code` 里的剩余音节匹配掉，真正的结束位置是 `match.end_pos`。

`match_extra_code` 是个递归函数：

[src/rime/dict/dictionary.cc:92-123](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L92-L123) 它把 `extra_code`（一个 `table::Code*`，即剩余的 `syllable_id` 序列）当作「待匹配的音节清单」，从 `current_pos` 开始，在 `syllable_graph.indices` 里逐音节向下找路径，返回「匹配得最远」的 `end_pos`。这就是把 u8-l3 讲的「长词收口进 TailIndex」在运行期重新接回音节图的桥梁。

核心的 `Table::Query`（BFS）：

[src/rime/dict/table.cc:571-630](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L571-L630) 用 `std::queue<pair<size_t, TableQuery>>` 做广度优先。第 584 行从 `syll_graph.indices` 取当前位置的所有音节；第 595-627 行的双重循环遍历「每个音节 × 该音节的每条属性（不同切法/模糊音）」。第 615 行 `Access` 取「到此为止的词条」记入 `result[end_pos]`；第 620-624 行若还能继续（`end_pos` 没到输入末尾），就 `Advance` 下钻一层推入队列，再 `Backdate` 回退以便循环处理兄弟音节。

注意第 612-614 行的 `quality_len` 计算：

```cpp
bool is_normal_spelling = props->type == kNormalSpelling;
double delta_quality_len =
    (is_normal_spelling ? 1.0 : 0.0) * (end_pos - current_pos);
```

只有「正常拼写」（非模糊/缩写/补全/纠错）的边才给 `quality_len` 加分，加的是这条边覆盖的字节长度。它衡量「这条路径里有多少输入是**完全精确匹配**的」，上层翻译器用它偏好「更多音节被精确命中」的整句。

`TableQuery` 的下钻机制（u8-l3 已讲索引结构，这里看运行期游标）：

[src/rime/dict/table.cc:152-183](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L152-L183) `Walk` 根据 `level_`（0/1/2）在 Head/Trunk/Trunk 三级数组里定位下一层索引：第 0 层用稠密下标 `lv1_index_->at[syllable_id]`（O(1)），第 1、2 层用 `find_node` 做二分（O(log n)）。第 178 行下钻到第 3 层时拿到的是 `tail()`（TailIndex）。

[src/rime/dict/table.cc:102-115](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L102-L115) `Advance` 在 `Walk` 成功后递增 `level_`、把当前音节压入 `index_code_`，并把 credibility/quality_len **累加**进栈——这是「沿路径相加」的实现，也是「对数空间」的好处。

最后看一眼调用方，确认这条链确实在运行期被走：

[src/rime/gear/script_translator.cc:459-460](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L459-L460) `script_translator` 在切好音节图后，正是 `dict->Lookup(syllable_graph, 0, &translator_->blacklist(), predict_word)` 这一行触发了上面整条链路。

#### 4.2.4 代码实践

**实践目标**：用 `test/dictionary_test.cc` 里的 `ScriptLookup` 测试，亲手验证 `Lookup` 返回的 collector 按 `end_pos` 分组、且 `code.size()` 随 `end_pos` 增长。

**操作步骤**：

1. 打开 [test/dictionary_test.cc:69-103](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/dictionary_test.cc#L69-L103)，阅读 `ScriptLookup` 测试。注意它的前置：第 18-29 行在 `SetUp` 里用 `DictCompiler` 现场编译了一本测试小词典（`dictionary_test.dict.yaml`），所以测试是自包含的。
2. 关注第 73-76 行：构造输入 `"shurufa"`，用 `Syllabifier::BuildSyllableGraph` 建图，断言 `interpreted_length == input_length`（说明整段输入都被解释了）。
3. 关注第 76 行 `dict->Lookup(g, 0)` 拿到 collector，然后第 79/88/95 行分别检查 `c->find(3)` / `c->find(5)` / `c->find(7)` 三个分组存在。
4. 对照下面这张「end_pos → code.size() → text 长度」的表格，把测试断言填进去：

   | `end_pos` | `code.size()`（音节数） | `text.length()`（UTF-8 字节数） | 对应词 |
   |-----------|------------------------|-------------------------------|--------|
   | 3 | 1（第 84 行断言） | 3（第 85 行断言） | 输 |
   | 5 | 2（第 93 行断言） | — | 输入 等 |
   | 7 | 3（第 100 行断言） | 9（第 101 行断言，3 个汉字 × 3 字节） | 输入法 |

**需要观察的现象**：`code.size()` 恰好等于「该候选跨越的音节数」，而 `end_pos` 等于「该候选在输入串里消费到的字节位置」。短词落在小的 `end_pos` 分组，长词落在大的 `end_pos` 分组。

**预期结果**：测试断言全部通过。第 102 行 `EXPECT_FALSE(d7.Next())` 说明 `end_pos=7` 分组里只有「输入法」一个 3 音节词（在这本小词典里）。

> 若你想实际运行：在构建时确保 `BUILD_TEST=ON`（u1-l2），执行 `ctest -R dictionary_test` 或直接 `./test/dictionary_test`。若环境无法编译，本实践作为「源码阅读型实践」同样成立——断言本身就是行为规格。**待本地验证**运行环境。

#### 4.2.5 小练习与答案

**练习 1**：`Table::Query` 为什么用 BFS（队列）而不是 DFS（递归）？

**参考答案**：BFS 把「状态」显式放进队列（`pair<size_t, TableQuery>`），每个 `TableQuery` 是一份可拷贝的游标状态。这样在分叉处（一个位置有多个后续音节），只需 `Advance` 后 `push` 进队列、再 `Backdate` 处理兄弟分叉，避免了 DFS 递归栈过深，也让「同 `end_pos` 的多条路径」自然汇合到 `result[end_pos]` 的同一个 `vector` 里。

**练习 2**：如果输入是 `xian`（既可切成 `xi/an`，也可整体切成 `xian`），`Table::Query` 会把「安」「先」「西安」分别放进哪些 `end_pos` 分组？

**参考答案**：音节图里会有边 `xi[0,2)`、`an[2,4)`、`xian[0,4)`（以及补全/纠错边）。BFS 在位置 0 取到 `xian` 单音节词（如「先」）记入 `result[4]`，沿 `xi` 下钻后在位置 2 再沿 `an` 取到双音节词（如「西安」）也记入 `result[4]`；而 `an` 单音节词（如「安」）要等位置 2 作为起点时才被 `Access` 到，记入 `result[4]`（因为 `an` 结束在位置 4）。也就是说多个不同切法只要结束位置相同，就会归入同一分组，由后续 `Sort()` 定序。

**练习 3**：`match_extra_code` 为什么必须返回「最远」的 `end_pos`，而不是第一个匹配成功就返回？

**参考答案**：一个长词的 `extra_code`（第 4 个及以后的音节）在音节图里可能有多种匹配路径（因为有模糊音/补全）。返回最远的 `end_pos` 意味着「让这个词尽可能多地消费输入」，这样它才会被归入正确的、更靠后的 `end_pos` 分组，与上层「按消费长度组织候选」的模型一致。代码第 119 行 `if (match.end_pos > best_match.end_pos) best_match = match;` 正是这个择优逻辑。

---

### 4.3 DictEntryCollector 与 DictEntryIterator：候选分组、排序与拉取

#### 4.3.1 概念说明

4.2 讲的是「如何把词条从码表里捞出来」，本讲讲「捞出来之后如何组织成可消费的候选流」。

这里有两个核心数据结构：

- **`Chunk`**（块）：码表里「一组同编码词条」的运行期表示。一个 Chunk 持有：指向 table 的指针、这条 `code`、词条数组 `entries` + 数量 `size` + 游标 `cursor`、可信度 `credibility`、全码长度积分 `quality_len`，以及（预测查询用的）`remaining_code`。它是 `DictEntryIterator` 的内部素材。
- **`DictEntryIterator`**：一个**惰性、按需排序**的候选迭代器，把若干 Chunk 串起来，对外提供 `Peek()`/`Next()`。它实现 `DictEntryFilterBinder`，可以挂过滤器（如黑名单）。

而 `DictEntryCollector = map<size_t, DictEntryIterator>`：每个 `end_pos` 分组对应一个 `DictEntryIterator`。所以「一次 `Lookup`」的产物，本质是「若干条独立的候选流，按结束位置分桶」。

这套设计的关键词是**拉模型（pull model）**和**增量排序**：候选不是一次性算完的，而是上层 `Peek` 一个才构造一个；排序也不是全排，而是用 `std::partial_sort` 每次只把「当前最优 Chunk」顶到队首。

#### 4.3.2 核心流程

`DictEntryIterator` 的消费流程：

```
AddChunk(chunk)            // 4.2 里 lookup_table 不断往里塞 Chunk
   │ （可塞多个 Chunk：来自不同 table / 不同切法路径）
   ▼
Sort()                     // partial_sort，把「队头候选最优」的 Chunk 顶到 chunk_index_
   │
   ▼  ┌──────────── 上层消费 ────────────┐
Peek()  │  取当前 Chunk 当前 cursor 处的词条  │
   │   │  临时构造一个 DictEntry：           │
   │   │    weight = e.weight - log(1e8)    │
   │   │           + chunk.credibility      │
   │   │    text   = table->GetEntryText(e) │
   │   └────────────────────────────────────┘
   ▼
Next()  // cursor++；若该 Chunk 耗尽则 chunk_index_++；再 Sort() 重排
```

排序的依据 `compare_chunk_by_head_element` 是三级比较：

1. **精确匹配 > 预测匹配**（`is_exact_match()` 优先）；
2. **`remaining_code` 更短的优先**（预测查询时，剩余码越短越接近用户输入）；
3. **权重更大的优先**（`credibility + 队头词条 weight` 降序）。

#### 4.3.3 源码精读

先看 `Chunk` 与比较函数：

[src/rime/dict/dictionary.cc:21-67](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L21-L67) `Chunk` 的多种构造函数重载体现了它的来源多样性：有的来自 `TableAccessor`（正向查询），有的来自单条 `table::Entry`（带 `matching_code_size`，长词）。第 64-66 行 `is_exact_match` / `is_predictive_match` 是排序的关键谓词。

[src/rime/dict/dictionary.cc:73-84](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L73-L84) 就是上面说的三级比较，注意第 82-83 行「按权重降序」用 `>`。

`AddChunk` 与 `Sort`：

[src/rime/dict/dictionary.cc:130-141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L130-L141) `AddChunk` 把 Chunk 推入 `query_result_->chunks` 并累加 `entry_count_`；`Sort` 用 `std::partial_sort` 只对 `[chunk_index_, chunk_index_+1)` 区间排序——也就是**只把最优的那个 Chunk 顶到当前位置**，其余暂不动。这是增量排序的灵魂：N 个 Chunk 里每次只花 O(N log N) 顶出一个，而非 O(N log N) 全排（每次 Next 都全排会很贵）。

最关键的 `Peek`：

[src/rime/dict/dictionary.cc:153-175](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L153-L175) 注意它**只在 `entry_` 为空时才构造**（第 154 行的 `if (!entry_ ...)`），实现了「同一个词条重复 Peek 不会重复构造」。第 163-164 行就是本讲反复强调的权重公式：

```cpp
const double kS = 18.420680743952367;  // log(1e8)
entry_->weight = e.weight - kS + chunk.credibility;
```

码表里存的 `e.weight` 是构建期写入的对数权重（u8-l4 讲过 `log(max(w, ε))`），减去 `log(1e8)` 做归一化，再加上沿音节图累加的 `chunk.credibility`（模糊音会扣分、纠错会重扣）。第 166-169 行处理预测查询：若该候选还有未匹配的剩余编码，把它写进 `comment`（如 `~ng`）并记下长度，供前端展示「这个词还差这几个字母」。

`Next` 与 `FindNextEntry`：

[src/rime/dict/dictionary.cc:177-201](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L177-L201) `FindNextEntry` 推进当前 Chunk 的 `cursor`，若该 Chunk 耗尽（第 182 行 `++chunk.cursor >= chunk.size`）则跳到下一个 Chunk（`++chunk_index_`），最后调 `Sort()` 把新的最优 Chunk 顶上来。`Next` 在外层包了过滤器循环（第 199 行 `filter_`），跳过被过滤的候选。

最后，`DictEntry` 本身定义在 [src/rime/dict/vocabulary.h:46-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/vocabulary.h#L46-L68)，字段 `text`/`comment`/`code`/`weight`/`quality_len`/`remaining_code_length`/`matching_code_size` 正好对应 `Peek` 里填的那些；`IsExactMatch`/`IsPredictiveMatch`（第 61-66 行）则复用了 `matching_code_size` 与 `code.size()` 的比较。

#### 4.3.4 代码实践

**实践目标**：通过修改一条测试断言，亲手验证 `Peek` 的权重计算与「拉模型」的惰性。

**操作步骤**：

1. 复制 [test/dictionary_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/dictionary_test.cc) 里的 `SimpleLookup` 测试到一个临时测试用例（示例代码，非项目原有代码）：

   ```cpp
   // 示例代码：观察权重与 credibility 的关系
   TEST_F(RimeDictionaryTest, PeekWeightDemo) {
     ASSERT_TRUE(dict_->loaded());
     rime::DictEntryIterator it;
     dict_->LookupWords(&it, "zhong", false);
     auto e = it.Peek();
     ASSERT_TRUE(bool(e));
     // 打印：text、code、weight
     std::cerr << "text=" << e->text
               << " code.size=" << e->code.size()
               << " weight=" << e->weight << "\n";
   }
   ```

2. 在 [dictionary.cc:164](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L164) 那一行临时加一行 `DLOG(INFO) << "weight=" << entry_->weight;`（注意 `DLOG` 只在 `ENABLE_LOGGING` 打开时编译，是项目原有惯例）。
3. 构建并运行该测试（**待本地验证**：需要 `ENABLE_LOGGING=ON` 才能看到 DLOG 输出）。

**需要观察的现象**：`weight` 是一个负数或较小正数（因为减去了 `log(1e8) ≈ 18.42`），而 `code.size() == 1`（`zhong` 是单音节）。

**预期结果**：同一词条多次 `Peek()` 返回同一个 `DictEntry` 指针（因为第 154 行 `if (!entry_)` 保证只构造一次）；`Next()` 之后 `entry_` 被 reset，下次 `Peek` 才构造下一个。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Sort` 用 `partial_sort(... , chunks.begin() + chunk_index_ + 1, ...)` 而不是 `sort` 全排序？

**参考答案**：因为 `DictEntryIterator` 是按需消费的，上层很可能只取前几个候选就停止了（候选菜单通常只显示一页）。`partial_sort` 只保证「区间内第 1 个位置是最优」，即只花代价把当前最优 Chunk 顶到队首；其余 Chunk 保持未完全排序，等真正需要时再排。全排序会把 N 个 Chunk 全排一遍，绝大多数排序结果根本不会被消费，纯属浪费。

**练习 2**：`Peek` 里为什么用 `chunk.table->GetEntryText(e)` 而不是直接存字符串？

**参考答案**：码表里的词条文本是以 `StringId`（marisa trie 索引）形式存储的（u8-l3），必须经过 `Table::GetEntryText` → `StringTable::GetString` 解引用才能得到 UTF-8 字符串。延迟到 `Peek` 才解引用，意味着「未被 Peek 的候选根本不付出字符串解引用的代价」——这是拉模型省 CPU 的另一面。

**练习 3**：`AddFilter`（第 143-151 行）挂上过滤器后，为什么要 `while (!exhausted() && !filter_(Peek()))` 循环？

**参考答案**：因为挂过滤器时，当前 `Peek` 出的候选可能恰好就被过滤掉了（比如在黑名单里）。这个循环主动跳过所有被过滤的队头候选，保证「挂完过滤器后，下一次对外 `Peek` 的一定是通过过滤的有效候选」。

---

### 4.4 字符串反查 LookupWords 与 syllable_id 还原 Decode

#### 4.4.1 概念说明

`Lookup` 的输入是「音节图」（已经有 `syllable_id`），适合拼音这种先切音节再查词的音码输入法。但有一类场景输入**不是音节图，而是一个原始字符串**：

- **形码输入法**（仓颉、五笔）：用户敲的 `abcd` 本身就是编码，不需要切音节，直接拿字符串去码表里查。
- **反查翻译器**（`reverse_lookup_translator`）：用户用拼音反查某个字的仓颉编码，输入是拼音串。

这时就要用 `LookupWords(str_code, predictive, ...)`。它的思路和 `Lookup` 相反：

- `Lookup`：音节图（已有 syllable_id）→ Table → 词条。
- `LookupWords`：字符串拼写 → **Prism** 反查 syllable_id → Table（`QueryWords`）→ 词条。

中间多了一步「Prism 把字符串翻译成 syllable_id」，这正是 Prism 存在的另一半价值。

`Decode` 则是 `LookupWords` 的逆操作之一：给一个 `Code`（syllable_id 序列），还原成可读的拼写串列表。它用在候选的 `comment`（注释）展示上——比如反查时给候选标注「这个词的编码是 `abc yz`」。

#### 4.4.2 核心流程

**`LookupWords` 流程**：

```
str_code (如 "zhong" 或 "z")
   │
   ├── predictive=false → Prism::GetValue(str_code)         // 精确匹配，1 个 spelling_id
   │                      返回 Match{value=spelling_id}
   │
   └── predictive=true  → Prism::ExpandSearch(str_code, limit) // 前缀扩展，多个 Match
                          返回 vector<Match>
   │  （Match.value 是 spelling_id，不是 syllable_id！）
   ▼
对每个 Match：Prism::QuerySpelling(match.value)
   │  SpellingAccessor 遍历 SpellingMap，得到 (syllable_id, type)
   │  只保留 type == kNormalSpelling 的（跳过模糊/缩写/补全）
   ▼
Table::QueryWords(syllable_id)
   │  返回 TableAccessor（该单音节下的所有词条）
   ▼
result->AddChunk({table, accessor, remaining_code})
```

一个关键细节：`Match.value` 是 **spelling_id**（拼写 id，trie 里存的值），不是 syllable_id。必须经 `QuerySpelling` 遍历 `SpellingMap` 才能得到真正的 syllable_id——这正是 u8-l2 强调的「拼写→音节」一对多关系。`remaining_code` 用于预测查询：若 `Match.length > str_code.length()`（trie 命中的拼写比输入长），把多出来的部分记为「剩余编码」，写进候选注释。

**`Decode` 流程**：对 `Code` 里每个 `syllable_id`，调 `primary_table()->GetSyllableById(id)` 得到拼写串，依次 push 进结果。任何一个查不到就整体失败返回 `false`。

#### 4.4.3 源码精读

`LookupWords`：

[src/rime/dict/dictionary.cc:299-349](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L299-L349) 第 307-315 行先按 `predictive` 分叉：精确匹配用 `prism_->GetValue`，预测用 `prism_->ExpandSearch`。第 318 行开始遍历每个 `Match`，第 319 行 `SpellingAccessor accessor(prism_->QuerySpelling(match.value))` 是「spelling_id → syllable_id」的关键一跳。第 324 行 `if (type > kNormalSpelling) continue;` 过滤掉所有非正常拼写（模糊音、缩写等）——这是 `LookupWords` 与 `Lookup` 的一个重要差异：`Lookup`（经音节图）会保留模糊音候选，而 `LookupWords` 只走精确拼写。第 326-331 行计算预测查询的 `remaining_code`。第 332-340 行对每张表调 `table->QueryWords(syllable_id)`，把结果作为 Chunk 加入。

`Table::QueryWords` 与 `GetSyllableById`：

[src/rime/dict/table.cc:550-553](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L550-L553) `QueryWords` 极简：新建一个 `TableQuery`，`Access(syllable_id)` 在第 0 层（HeadIndex）稠密下标取该单音节下的词条。这正是 u8-l3 讲的「HeadIndex 按 syllable_id O(1) 下标」的运行期用法。

[src/rime/dict/table.cc:543-548](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/table.cc#L543-L548) `GetSyllableById` 通过 `syllabary_->at[syllable_id]` 取 `StringType`，再 `GetString` 解出拼写串——`Decode` 就靠它。

`Decode`：

[src/rime/dict/dictionary.cc:351-362](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L351-L362) 逻辑直白：遍历 `code`，每个 id 调 `GetSyllableById`，空串即失败。注意它**只用主表**（`primary_table()`）——因为 syllabary（音节表）在主表与各 pack 里是一致的，没必要查附加表。

调用方印证：

- [src/rime/gear/table_translator.cc:268](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L268) 形码翻译器用 `dict_->LookupWords(&iter, code, false, ...)` 精确查编码。
- [src/rime/gear/reverse_lookup_translator.cc:175](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/reverse_lookup_translator.cc#L175) 反查翻译器用 `LookupWords(&iter, code, true, 100, nullptr)` 预测查（限 100 条）。
- [src/rime/gear/script_translator.cc:311](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L311) `script_translator` 用 `dict_->Decode(code, &syllables)` 把候选编码还原成拼音展示。

#### 4.4.4 代码实践

**实践目标**：用 `SimpleLookup` 与 `PredictiveLookup` 两个测试，对比精确查询与预测查询的差异，并验证 `Decode` 的往返一致性。

**操作步骤**：

1. 阅读 [test/dictionary_test.cc:45-67](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/dictionary_test.cc#L45-L67) 两个测试。
2. **`SimpleLookup`**（第 48 行）：`LookupWords(&it, "zhong", false)` 精确查 `zhong`，断言取到「中」，`code.size() == 1`；第 53 行 `dict_->Decode(it.Peek()->code, &raw_code)` 把 code 还原成 `"zhong"`——一次「拼写 → syllable_id（经 Prism）→ code（经 Table）→ 拼写（经 Decode）」的完整往返。
3. **`PredictiveLookup`**（第 60 行）：`LookupWords(&it, "z", true)` 用前缀 `z` 预测查。注意断言第 62 行期望取到的是「咋」、第 66 行 `Decode` 出来是 `"za"`——而不是 `"z"` 本身。这说明：输入 `z` 经 `ExpandSearch` 命中了多个以 `z` 开头的拼写，取到的最优候选恰好是音节 `za`，所以 `remaining_code` 应为 `"a"`（即 `za` 比 `z` 多出的部分）。
4. 在 [dictionary.cc:326-331](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/dict/dictionary.cc#L326-L331) 处对照确认 `remaining_code` 的计算逻辑。

**需要观察的现象**：预测查询返回的候选 `text` 不一定是「以输入串本身为完整编码」的词；`Decode` 还原出的拼写可能比输入串长（`z` → `za`），多出来的部分就是 `remaining_code`。

**预期结果**：两个测试均通过。`SimpleLookup` 验证往返一致性（`zhong` → `zhong`），`PredictiveLookup` 验证前缀扩展（`z` 命中 `za`）。

#### 4.4.5 小练习与答案

**练习 1**：`LookupWords` 为什么要 `if (type > kNormalSpelling) continue;` 过滤掉模糊音？

**参考答案**：`LookupWords` 的典型场景是形码反查（按精确编码查词），用户输入的字符串就是想查的精确拼写。模糊音/缩写是「拼写代数」派生出的等价拼写，会让结果混入大量用户没打算查的音节。保留它们会污染结果集，所以只取 `kNormalSpelling`（原始拼写）。对比之下，`Lookup(SyllableGraph)` 走音节图，模糊音候选是用户期望的（拼写代数在 Prism/音节图构建期就已生效），所以那条路径不过滤。

**练习 2**：`Decode` 只用 `primary_table()`，为什么不去查 packs？

**参考答案**：`Decode` 解的是 `syllable_id` → 拼写串，依赖 `Syllabary`（音节表）。`Syllabary` 描述的是「这本词典认识哪些音节」，主表与各 pack 共享同一套音节编号（packs 在构建时是追加到同一 syllabary 的），所以查主表的 syllabary 即可覆盖所有 pack 的音节。去 packs 里查是多余的。

**练习 3**：`LookupWords` 返回值是「匹配到的 key 数量」（`return keys.size()`），而不是「加入的候选数」。这两者何时会不相等？

**参考答案**：经常不相等。一个 `Match`（一个 spelling_id）经 `QuerySpelling` 可能展开成多个 syllable_id（SpellingMap 的一对多），每个 syllable_id 经 `QueryWords` 又可能取到多个词条，再乘以表的数量（主表 + packs）。所以「候选数」通常远大于「key 数」。返回 key 数是为了让调用方判断「字符串是否命中了任何拼写」（例如 `table_translator.cc:196` 用它判断是否需要降级查询用户词典）。

---

## 5. 综合实践

把本讲四个模块串起来，设计一个**端到端的数据流追踪任务**，用输入 `"shurufa"` 走完从按键到候选的全链路。

**任务**：在一张纸上（或 Markdown 里）画出下面的完整数据流，并标注每一步涉及的源码位置与关键数据结构。

```
用户输入 "shurufa"
   │
   ▼  Syllabifier::BuildSyllableGraph（u7-l3，用 Prism）
SyllableGraph g
   ├─ vertices:  {0, 2, 5, 7, ...}
   ├─ edges:     0 -> {2 -> {shu, ...}, ...}  ...
   ├─ indices:   {0 -> {shu_id -> [props], ...}, ...}
   └─ interpreted_length == 7
   │
   ▼  dict->Lookup(g, 0)                         [dictionary.cc:271]
   │     │  对主表 + 每个 pack：                 [dictionary.cc:279]
   │     ▼  table->Query(g, 0, &result)          [table.cc:571] BFS 沿 indices 走
   │     │     Access 取词 → result[end_pos]     [table.cc:615]
   │     │     Advance 下钻 → 推入队列            [table.cc:621]
   │     ▼  lookup_table: TableQueryResult → DictEntryCollector  [dictionary.cc:238]
   │           短词：(*collector)[end_pos].AddChunk(...)
   │           长词(>3 音节)：match_extra_code 接回音节图       [dictionary.cc:92]
   │     ▼  对每个 end_pos 分组 Sort() + 可选黑名单              [dictionary.cc:288]
   ▼
DictEntryCollector (map<size_t, DictEntryIterator>)
   ├─ [3] -> DictEntryIterator { Chunk(输) }
   ├─ [5] -> DictEntryIterator { Chunk(输入, ...) }
   └─ [7] -> DictEntryIterator { Chunk(输入法, ...) }
   │
   ▼  上层 script_translator 消费：Peek()/Next()   [dictionary.cc:153/193]
   │     临时构造 DictEntry：
   │       weight = e.weight - log(1e8) + credibility
   │       text   = table->GetEntryText(e)         [table.cc:632]
   │
   ▼  反向辅助：dict->Decode(code, &syllables)     [dictionary.cc:351]
         把 "输入法" 的 code(3 个 syllable_id) 还原成 ["shu","ru","fa"]
         写进候选 comment 供展示
```

**进阶子任务**：

1. 在图上标出「同一个 `end_pos` 分组里的候选可能来自不同 table（主表 + pack）、不同切法路径」，并解释为什么它们能被合并进同一个 `DictEntryIterator`。
2. 对比：如果 `Lookup` 改成 `LookupWords`（字符串反查）路径，上图哪些步骤会消失、哪些会新增？（答案：消失 `Syllabifier::BuildSyllableGraph` 与 `Table::Query` 的 BFS；新增 `Prism::GetValue/ExpandSearch` 与 `QuerySpelling`，最后用 `Table::QueryWords` 单层下标取词。）
3. 写一段话解释：为什么 `DictEntryCollector` 选 `map<size_t, ...>`（按 `end_pos` 排序的有序映射）而不是 `unordered_map`？（提示：上层翻译器会按 `end_pos` 从大到小 `rbegin()` 遍历，优先消费「更长的整句候选」，见 [script_translator.cc:472](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L472) 的 `phrase_->rbegin()`。）

> 本综合实践为「源码阅读型实践」，不依赖运行环境；若要运行验证，可基于 `test/dictionary_test.cc` 的 `ScriptLookup` 扩展断言。**待本地验证**运行环境。

## 6. 本讲小结

- `Dictionary` 是 **Prism（音节索引）+ Table（码表）** 的门面，本身是一个注册为 `"dictionary"` 的组件，由 `DictionaryComponent` 按方案配置装配；同名 Prism/Table 经 `weak_ptr` 缓存在进程内共享，避免重复 mmap。
- 三条查询接口对应三种输入：`Lookup(SyllableGraph)` 正向查询（音码主力）、`LookupWords(字符串)` 反查（形码/反查翻译器）、`Decode(Code)` 把 syllable_id 还原成拼写。
- `Lookup` 的核心是 `Table::Query`：用一个「`(位置, TableQuery 状态)`」队列沿音节图做 **BFS**，在 `indices` 表的每个分叉处 `Access` 取词、`Advance` 下钻，产出 `TableQueryResult`（按 `end_pos` 分组的 `TableAccessor` 列表）。
- `lookup_table` 把 `TableQueryResult` 翻译成 `DictEntryCollector`，并特别用 `match_extra_code` 把 `TailIndex` 长词（>3 音节）的 `extra_code` 接回音节图，确定其真实结束位置。
- `DictEntryCollector = map<size_t, DictEntryIterator>`，按「候选消费到的字节位置」分桶；`DictEntryIterator` 内部由若干 `Chunk` 组成，用 `partial_sort` 增量排序 + 拉模型 `Peek`，候选权重为 `e.weight - log(1e8) + credibility`。
- `LookupWords` 比 `Lookup` 多一步「Prism 把字符串翻译成 syllable_id」（且只保留 `kNormalSpelling`），再用 `Table::QueryWords` 在 HeadIndex 第 0 层 O(1) 取词。

## 7. 下一步学习建议

本讲讲完了**静态词典**（`.table.bin`）的运行期查询。接下来推荐：

- **u8-l6 用户词典与 user_db**：`UserDictionary` 复用了本讲的 `DictEntryCollector` / `DictEntryIterator` 模型，但底层换成了 LevelDB 而非 mmap 码表，并且会动态更新权重（提交次数、tick）。学完本讲再看 u8-l6，你会清楚地看到「静态查询」与「动态学习」两套机制的对称与差异。
- **回看 u6-l4 Translator 组件族**：带着本讲的知识重读 `script_translator.cc`，重点看它如何用 `phrase_->rbegin()` 按 `end_pos` 逆序消费 collector、如何把用户词典结果（`user_phrase_`）与静态词典结果（`phrase_`）合并排序。
- **延伸阅读**：如果想深入「权重如何在整句层面累加」，可读 [src/rime/gear/translator_commons.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h) 里 `Sentence` 如何沿音节图把多个 `DictEntry` 的 `weight` 与 `quality_len` 拼成整句评分——那是本讲 `credibility`/`quality_len` 字段的真正消费处。
