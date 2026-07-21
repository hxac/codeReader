# u6-l4 Translator 组件族

## 1. 本讲目标

在上一篇（u6-l3）里我们看到：Segmentor 把输入串切成一段段 `Segment`，并给每段打上 tag（`abc`/`raw`/`punct`/`pinyin`/`cangjie`……）。从这一段「带 tag 的输入」到「一串候选词」的跨越，正是 **Translator（翻译器）** 的职责。

学完本讲，你应当能够：

- 说清 **Translator 基类契约**：`Query(input, segment)` 返回什么、tag 如何做门控。
- 区分两大主力翻译器：**`script_translator`**（音节图驱动，适合拼音）与 **`table_translator`**（码表驱动，适合仓颉/五笔等形码）。
- 理解 `translator_commons.h` 提供的共享积木：`TranslatorOptions`、`Phrase`、`Sentence`，以及它们为何能被两类翻译器复用。
- 认识一整套**辅助翻译器**：`reverse_lookup_translator`、`schema_list_translator`、`switch_translator`、`punct_translator`、`echo_translator`、`history_translator`，知道它们各自在什么场景被触发。
- 读懂数据驱动装配：同一个方案里 `script_translator` 与 `table_translator@cangjie` 为何能并存。

## 2. 前置知识

本讲建立在以下已建立的认知之上（不重复展开）：

- **组件机制**（u5-l1/u5-l2）：`Translator` 继承自 `Class<Translator, const Ticket&>`，以 `Ticket` 构造，由 `Registry` 按名 `Require` 后 `Create`。
- **Ticket 拆解**（u5-l4）：处方串 `klass@alias` 中，`@` 左侧是注册表里的类名 `klass`，右侧覆盖默认命名空间 `name_space`。
- **Segmentation/Segment**（u6-l3）：`Segment` 有 `[start, end)` 区间、`tags` 集合、`status` 四态；tag 是 segmentor 与 translator 之间的暗号。
- **Translation/Candidate**（u3-l3）：`Query` 返回惰性迭代器 `an<Translation>`，`Peek()/Next()` 拉取候选；`Candidate` 有 `text/comment/preedit/start/end/type/quality`。
- **Menu 拉模型**（u3-l4）：各 translator 的输出经 `MergedTranslation` 归并、Filter 链过滤，再按需 `Prepare` 取候选。

如果一个 `Segment` 被 segmentor 标记为某种 tag，那么引擎会拿着这段输入依次询问方案 `engine/translators` 清单里的**每一个** translator；每个 translator 用 `segment.HasAnyTagIn(tags_)` 判断「这段归不归我管」，归我管才返回非空 `Translation`。本讲的核心就是回答：「归我管之后，候选从哪里来。」

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/rime/translator.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h) | `Translator` 抽象基类，定义 `Query` 纯虚函数。 |
| [src/rime/gear/translator_commons.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.cc) | 共享积木：`TranslatorOptions`、`Phrase`、`Sentence`、`Spans`。 |
| [src/rime/gear/memory.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/memory.cc) | `Memory` 混入类：持有 `dict_`/`user_dict_`，监听提交信号做用户词典学习。 |
| [src/rime/gear/script_translator.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc) | 拼音主力翻译器：音节图 + 用户词典 + 造句。 |
| [src/rime/gear/table_translator.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc) | 形码主力翻译器：码表前缀匹配 + 惰性扩展 + 编码器。 |
| [src/rime/gear/reverse_lookup_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/reverse_lookup_translator.cc) | 反查翻译器：用一个词典查码、用另一个词典反查候选注释。 |
| [src/rime/gear/schema_list_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/schema_list_translator.cc) / [switch_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc) | 仅在切换器（Switcher）引擎里生效，列出方案/开关。 |
| [src/rime/gear/punctuator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/punctuator.cc) | `PunctTranslator`：把 `punct`/`punct_number` 段翻译成标点候选。 |
| [src/rime/gear/echo_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/echo_translator.cc) / [history_translator.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/history_translator.cc) | 兜底回显与历史复用。 |
| [src/rime/gear/gears_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc) | 所有翻译器的注册入口。 |
| [data/minimal/luna_pinyin.schema.yaml](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml) | 综合实践的配置样本。 |

## 4. 核心概念与源码讲解

### 4.1 Translator 基类契约与「按 tag 门控」

#### 4.1.1 概念说明

`Translator` 是流水线四类组件中最「重」的一类：它的产出直接就是用户看到的候选词。但它的接口本身极简——只有一个纯虚函数 `Query`，输入是「待翻译的字符串 + 这段输入所在的 `Segment`」，输出是一个**惰性候选流** `an<Translation>`。

`Translator` 基类只持有两个字段：`engine_`（引擎指针，用于拿 schema、context）和 `name_space_`（配置命名空间，决定从方案的哪一节读配置）。这两个字段直接来自 `Ticket`，是所有组件的统一构造契约。

#### 4.1.2 核心流程

一个 translator 被调用时的典型判断顺序：

1. **tag 门控**：用 `segment.HasAnyTagIn(tags_)` 判断这段输入是否属于自己的管辖范围；不属于则立即返回 `nullptr`，把机会让给清单里的下一个 translator。
2. **资源就绪检查**：依赖的词典是否已加载（`dict_->loaded()`）。
3. **查词典**：用各自的方式（音节图 / 字符串前缀）从系统词典与用户词典拉取候选迭代器。
4. **包装成 Translation**：把迭代器包成惰性 `Translation`，必要时叠加 `DistinctTranslation`（去重）、`CharsetFilterTranslation`（字符集过滤）等装饰器。
5. **返回**：交由引擎的 `Menu` 归并、Filter 过滤后呈现给前端。

`tags_` 的默认值是 `{"abc"}`（在 `TranslatorOptions` 里设定，见 4.2），意味着「默认只翻译 abc 段」。方案里写 `table_translator@cangjie` 时，`cangjie` 节里有 `tag: cangjie`，于是这个 translator 的 `tags_` 被改成 `{"cangjie"}`，只认领带 `cangjie` tag 的段。

#### 4.1.3 源码精读

`Translator` 基类定义在 [src/rime/translator.h:L22-L36](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translator.h#L22-L36)，核心是第 28–29 行的纯虚 `Query`：

```cpp
virtual an<Translation> Query(const string& input,
                              const Segment& segment) = 0;
```

构造函数仅从 `Ticket` 取两个字段：

```cpp
explicit Translator(const Ticket& ticket)
    : engine_(ticket.engine), name_space_(ticket.name_space) {}
```

注意 `name_space_` 是「配置入口名」。比如处方串 `script_translator@pinyin` 中 `@` 右侧的 `pinyin` 会覆盖默认命名空间，详见 [src/rime/ticket.cc:L19-L23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/ticket.cc#L19-L23)：

```cpp
size_t separator = klass.find('@');
if (separator != string::npos) {
  name_space = klass.substr(separator + 1);
  klass.resize(separator);
}
```

所有翻译器在 `gears` 模块初始化时注册，见 [src/rime/gear/gears_module.cc:L67-L77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gears_module.cc#L67-L77)。其中 `r10n_translator` 是 `script_translator` 的别名，`fluency_editor` 那类「一个类多个注册名」的手法在这里也出现了一次。

#### 4.1.4 代码实践

**实践目标**：建立一个全局印象——方案 `engine/translators` 清单里每一行处方串，最终都落在 `gears_module.cc` 的某一行 `Register` 上。

**操作步骤**：

1. 打开 `data/minimal/luna_pinyin.schema.yaml`，定位 `engine/translators`（第 58–63 行）。
2. 对清单里的每个名字（`punct_translator`、`reverse_lookup_translator`、`script_translator`、`table_translator@cangjie`、`script_translator@pinyin`），到 `src/rime/gear/gears_module.cc` 第 67–77 行找到对应的 `Register` 调用。
3. 注意 `@cangjie`、`@pinyin` 后缀：它们不是新的类名，而是覆盖了 `name_space_`，使同一个 `ScriptTranslator`/`TableTranslator` 类可以读取方案里不同的配置节。

**需要观察的现象**：清单里出现了两次 `script_translator`（一次裸名、一次 `@pinyin`），却只对应 `gears_module.cc` 里**一行** `Register("script_translator", ...)`。这印证了「类少、实例多」的数据驱动装配思想。

**预期结果**：每个处方串的 `klass` 部分都能在注册表里找到唯一对应的类；`@alias` 部分仅决定配置命名空间。

#### 4.1.5 小练习与答案

**练习 1**：方案里写 `table_translator@cangjie`，运行时这个 `TableTranslator` 实例的 `name_space_` 等于什么？它会去读方案里的哪个配置节？

**答案**：`name_space_` 等于 `"cangjie"`，因此它读取 `cangjie:` 这一节（如 `cangjie/dictionary`、`cangjie/tag`、`cangjie/prefix`）。对照 `luna_pinyin.schema.yaml` 第 100–110 行的 `cangjie:` 节即可印证。

**练习 2**：为什么 `gears_module.cc` 里 `script_translator` 只注册一次，方案里却能出现多个 `script_translator@xxx` 实例？

**答案**：因为 `Register` 注册的是「工厂」（`Component<ScriptTranslator>`），`Require(klass)->Create(ticket)` 可以用不同的 `ticket.name_space` 反复实例化出多个对象，各读各的配置节。注册一次、实例多次。

---

### 4.2 translator_commons：共享积木 TranslatorOptions / Phrase / Sentence

#### 4.2.1 概念说明

拼音和形码虽然查词典的方式天差地别，但有大量「公共配置」和「公共数据结构」是相同的：分隔符、是否开启补全、preedit/comment 的格式化器、黑名单、候选如何承载一个词条……librime 把这些共享部分抽到 `translator_commons.h`，形成三块积木：

- **`TranslatorOptions`**：一揽子翻译器选项。`ScriptTranslator` 和 `TableTranslator` 都**多重继承**它（连同 `Memory`），从而共享同一套配置读取逻辑。
- **`Phrase`**：`Candidate` 的子类，内部持有一个 `DictEntry`（词典条目），是「一个候选词」的标准载体。script 和 table 产出的候选几乎都是 `Phrase`。
- **`Sentence`**：`Phrase` 的子类，表示由多个词拼成的「整句候选」，额外记录 `components_`（组成词）和 `word_lengths_`（各词在输入串中占的长度）。

#### 4.2.2 核心流程

`TranslatorOptions` 的构造函数统一从方案的 `name_space + "/xxx"` 读取配置，关键项包括：

- `delimiters_`：音节分隔符（默认取 `speller/delimiter`，回退到空格）。
- `tags_`：本翻译器认领哪些 tag，默认 `{"abc"}`；可由 `tag:` 单值或 `tags:` 列表覆盖。
- `enable_completion_`：是否允许补全（输入前缀也能出候选），默认 `true`。
- `strict_spelling_`：严格拼写，默认 `false`。
- `initial_quality_`：给本翻译器所有候选一个固定加成，用于在不同翻译器之间调优先级。
- `preedit_formatter_` / `comment_formatter_`：两个 `Projection`，对 preedit/comment 文本做拼写代数式的字符串变换（如把 `v` 显示成 `ü`）。
- `blacklist_`（`dictionary_exclude`）：要排除的词条集合。

`Phrase` 的核心是它**持有 `an<DictEntry> entry_`**：text、comment、preedit、weight、code 全部来自这个 entry。这样候选只是词典条目的一层薄包装，查询结果可以零拷贝地变成候选。

#### 4.2.3 源码精读

`TranslatorOptions` 的字段与访问器集中在 [src/rime/gear/translator_commons.h:L143-L185](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h#L143-L185)，其中第 174 行 `vector<string> tags_{"abc"};` 是「默认只认 abc 段」的不变式，`set_tags` 还保证它永不为空（见第 150–155 行）。

配置读取逻辑在 [src/rime/gear/translator_commons.cc:L115-L168](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.cc#L115-L168)。其中 tag 的加载（第 140–153 行）有讲究：若配置里有 `tag:` 单值，则替换首个 tag 并把 `tags:` 列表当作**额外** tag 追加；若没有 `tag:`，则用 `tags:` 列表整体替换默认值；两者都没有时回退到 `{"abc"}`。

`Phrase` 类定义在 [src/rime/gear/translator_commons.h:L63-L110](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h#L63-L110)。它继承 `Candidate`，构造时传入 `language`、`type`、`start`、`end` 和一个 `DictEntry`。`text()`/`comment()`/`preedit()` 直接转发给 `entry_`（第 71–73 行）：

```cpp
const string& text() const { return entry_->text; }
string comment() const { return entry_->comment; }
string preedit() const { return entry_->preedit; }
```

第 102–104 行的 `spans()` 把「音节边界」计算委托给 `PhraseSyllabifier`（script_translator 会注入一个），用于光标按音节跳转。

`Sentence` 类在 [src/rime/gear/translator_commons.h:L114-L137](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h#L114-L137)，它的 `Extend` 方法（[translator_commons.cc:L94-L106](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.cc#L94-L106)）把一个新词追加进整句：文本拼接、编码拼接、记录词长、更新权重和 end 区间。这是造句（Poet）算法逐词扩展时的核心动作。

#### 4.2.4 代码实践

**实践目标**：验证 `Phrase` 只是 `DictEntry` 的薄包装，理解 candidate 与词典条目的关系。

**操作步骤**：

1. 阅读 [src/rime/gear/translator_commons.h:L63-L110](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h#L63-L110) 的 `Phrase` 类。
2. 在 `script_translator.cc` 中搜索 `New<Phrase>`，你会看到第 643 行和第 661 行两处构造：分别对应用户词候选与系统词候选。
3. 注意这两处都把 `entry`（一个 `DictEntry`）作为最后一个参数传给 `Phrase`，候选的 text/weight/code 全部来自它。

**需要观察的现象**：`Phrase` 自身几乎不存数据，所有可读属性都是对 `entry_` 的转发；`set_comment` / `set_preedit` 则是回写 `entry_`（第 74–75 行）。

**预期结果**：理解「候选 = 区间 [start,end) + 类型 + 一个 DictEntry 指针」。这也是为什么同一个 `DictEntry` 可以被多个候选共享、而修改候选注释会反映回 entry。

#### 4.2.5 小练习与答案

**练习 1**：`TranslatorOptions` 的 `tags_` 默认值是什么？为什么这个默认值能让一个未做任何 tag 配置的 `script_translator` 自动认领 abc 段？

**答案**：默认 `{"abc"}`（[translator_commons.h:L174](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h#L174)）。因为 `abc_segmentor`（见 u6-l3）正是给主拼音段打 `abc` tag，默认 tag 与之对齐，无需额外配置就能匹配。

**练习 2**：`Phrase` 和 `Sentence` 是什么关系？`Sentence` 比 `Phrase` 多记录了什么？

**答案**：`Sentence` 公有继承 `Phrase`（[translator_commons.h:L114](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/translator_commons.h#L114)）。它额外记录 `components_`（拼成该句的各 `DictEntry`）和 `word_lengths_`（各词在输入串中的长度），用于整句的 preedit 切分与按词光标移动。

---

### 4.3 script_translator：音节图驱动的拼音翻译器

#### 4.3.1 概念说明

`ScriptTranslator` 是为**拼音这类音码输入**设计的。拼音输入有个核心难题：一串字母 `xian` 可以切分成 `xi/an`、`xia/n`（无效）、`xian` 等多种音节组合，每种组合都对应不同的词。所以「先把输入串切成所有可能的音节序列」是查词的前提。

为此，`ScriptTranslator` 的工作分两步：

1. **建音节图（SyllableGraph）**：调用 `Syllabifier`（见 u7-l3）借助 `Prism` 把输入串展开成一张有向图，图里每条边代表「从位置 A 到位置 B 是某个音节」。
2. **沿图查词典**：把音节图交给 `Dictionary::Lookup(syllable_graph, ...)`，词典沿图里的每条路径查 `Table`，得到按 `end_pos` 分组的候选。

「script（脚本）」之名正源于此——它不是把输入当作一个死板的码，而是当作一段可以多种方式「念」出来的脚本。

#### 4.3.2 核心流程

`ScriptTranslator::Query` 的流程：

1. tag 门控 + 词典就绪检查。
2. 创建 `ScriptTranslation` 对象（真正的候选流），传入 corrector、poet、input、start、end_of_input。
3. 调 `ScriptTranslation::Evaluate(dict, user_dict)`：
   - `BuildSyllableGraph(prism)` 生成音节图，返回 `consumed`（被图覆盖的输入长度）。
   - `dict->Lookup(syllable_graph, ...)` 得到系统词 `phrase_`（`DictEntryCollector`，按 end_pos 分组）。
   - `user_dict->Lookup(syllable_graph, ...)` 得到用户词 `user_phrase_`。
   - 若无可靠精确匹配且音节 ≥ 2，调用 `MakeSentence`/`MakeSentences` 造整句候选。
4. 用 `DistinctTranslation` 去重后返回。

候选的优先级在 `PrepareCandidate` 里决定，核心是「**编码长度长的优先**」（更长意味着吃掉更多输入、更精确），并辅以「用户词优先于系统词」的偏置。每个候选的 quality 按下式计算：

\[
\text{quality} = \exp(\text{entry.weight}) + \text{initial\_quality} + \frac{\text{entry.quality\_len}}{\text{full\_code\_length}}
\]

其中 `exp(weight)` 把词典里存储的对数权重还原为线性权重（对数存储是为了造句时权重的连乘变成连加）。

#### 4.3.3 源码精读

`ScriptTranslator` 采用三重继承：`Translator` + `Memory` + `TranslatorOptions`，见 [src/rime/gear/script_translator.h:L27-L29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.h#L27-L29)。构造函数 [src/rime/gear/script_translator.cc:L182-L205](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L182-L205) 在 `TranslatorOptions` 读到的通用选项之外，还额外读 `spelling_hints`、`max_word_length`、`enable_correction`、`max_homophones` 等 script 专属项，并按需创建 `Corrector`（纠错器）和 `Poet`（造句器）。

`Query` 主体在 [script_translator.cc:L207-L235](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L207-L235)。第 211 行是 tag 门控；第 218–219 行决定是否启用用户词典（受 `IsUserDictDisabledFor` 按 pattern 禁用）；第 223–229 行创建并 `Evaluate` 候选流；第 230 行套一层 `DistinctTranslation` 去重。

`Evaluate` 在 [script_translator.cc:L453-L510](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L453-L510)，关键三步：第 454 行建音节图、第 459–460 行查系统词、第 464–466 行查用户词。第 482–488 行用 `has_exact_match_phrase` + `is_correction_match` 判断「是否有可靠精确匹配」，第 496–507 行据此决定是否造句——这是「精确匹配优先于整句联想」的策略。

候选选取的优先级逻辑在 `PrepareCandidate`，见 [script_translator.cc:L591-L673](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L591-L673)。它比较 `user_phrase_code_length` 与 `phrase_code_length`（即用户词/系统词吃掉的输入长度），用 `prefer_user_phrase`（第 582–589 行）在长度相同时偏置用户词。quality 的计算就在第 646–648 行（用户词）与第 664–666 行（系统词），正是上面公式。

`Peek` 在 [script_translator.cc:L560-L576](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L560-L576)，负责在首次 peek 时懒填 preedit（`GetPreeditString`）和注释（`GetOriginalSpelling`，受 `spelling_hints` 控制），并把 `ScriptSyllabifier` 挂到候选上以支持光标按音节跳转。

#### 4.3.4 代码实践

**实践目标**：跟踪一次拼音查询，看音节图如何决定候选。

**操作步骤**：

1. 在 `ScriptTranslator::Query`（[script_translator.cc:L207](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/script_translator.cc#L207)）入口处想象 `input = "xian"`。
2. 顺着第 223 行进入 `ScriptTranslation`，再到 `Evaluate`（第 453 行）。
3. 注意第 454 行 `BuildSyllableGraph` 会把 `xian` 展开成多条边（如 `xi`→`an`、`xi'an` 整体等），这正是 u7-l3 讲的 SyllableGraph。
4. 第 459 行 `dict->Lookup(syllable_graph, 0, ...)` 沿这些边查词典，得到「先」「西安」「线」等候选，分别对应不同的 end_pos。

**需要观察的现象**：同一个 `xian` 会同时产生「单音节词（先）」和「双音节词（西安）」两类候选，它们在 `phrase_`（`DictEntryCollector`）里按 end_pos 分桶，end_pos 越大说明吃掉的输入越多。

**预期结果**：候选列表里既有覆盖 4 个字母的「西安」（`xi'an`），也有只覆盖 3 个字母的「先」（`xian` 切成 `xi`+…）；`PrepareCandidate` 让编码更长的候选排在前面。

> 待本地验证：若你已按 u1-l5 编译出 `rime_api_console`，可输入 `xian` 观察候选顺序；若未编译，以上为源码阅读型结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `script_translator` 要用 `exp(weight)` 还原权重，而不是直接用词典里存的 `weight`？

**答案**：词典/用户词典里 weight 以**对数**存储，目的是造句时把多词联合概率的连乘 \( \prod w_i \) 变成对数域的连加 \( \sum \log w_i \)。在单个候选排序时再 `exp` 还原为线性值，便于和 `initial_quality` 等线性项相加。

**练习 2**：`Evaluate` 第 496 行的判断「`has_at_least_two_syllables && !has_reliable_phrase && !has_reliable_user_phrase`」想表达什么策略？

**答案**：只有当「输入至少含两个音节」且「系统词和用户词都没有精确匹配」时，才启动造句（`MakeSentence`）。即**精确匹配优先于整句联想**——如果有现成的词能精确覆盖输入，就不浪费算力去造句。

---

### 4.4 table_translator：码表驱动的形码翻译器

#### 4.4.1 概念说明

`TableTranslator` 是为**仓颉、五笔这类形码输入**设计的。形码的特点是：输入串本身就是「码」，不存在「多种切分」的问题——`abcd` 就是去码表里查 `abcd` 这个码（或以 `abcd` 为前缀的码）。所以它不需要音节图，而是直接做**字符串前缀匹配**。

「table」之名来自 `Table`（码表，见 u8-l3）：从 `syllable_id` 序列到词条的多级索引。形码方案里，每个字/词对应一个确定的编码序列。

#### 4.4.2 核心流程

`TableTranslator::Query` 的流程：

1. tag 门控（无词典加载检查也能进，因为有造句兜底）。
2. 把输入末尾的分隔符 trim 掉得到 `code`。
3. 若 `enable_completion_`：创建 `LazyTableTranslation`——**惰性**地分批查表，先查 10 条，不够再按 10 倍因子扩展。
4. 否则一次性 `LookupWords` 精确查（`completion=false`）。
5. 可选地套 `CharsetFilterTranslation`。
6. 若候选为空且 `enable_sentence_`：调用 `MakeSentence` 造句兜底；或 `sentence_over_completion_` 时把整句插到补全候选之前。
7. 套 `DistinctTranslation` 去重后返回。

形码特有的两个机制：

- **`UnityTableEncoder`**：当用户输入了一个码表里没有的多字词（用户自造词），编码器会按 `encoder/rules` 公式（如 `AaZa`）为这个词生成编码并存进用户词典，下次即可直接输入。候选注释里会出现 `☯` 符号（`kUnitySymbol`）标记这是编码器构造的词条。
- **`MakeSentence`**：用 BFS 在「词图」上找一条能覆盖整段输入的路径，把多个形码字拼成一句。与 script 不同，这里的「词图」是用 `Prism::CommonPrefixSearch` 逐位置前缀搜索构建的。

#### 4.4.3 源码精读

`TableTranslator` 同样三重继承 `Translator` + `Memory` + `TranslatorOptions`，见 [src/rime/gear/table_translator.h:L25-L50](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.h#L25-L50)。构造函数 [table_translator.cc:L211-L235](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L211-L235) 读取 table 专属项：`enable_charset_filter`、`enable_sentence`、`sentence_over_completion`、`enable_encoder`、`encode_commit_history`、`max_phrase_length`、`max_homographs`，并在 `enable_encoder_` 且有用户词典时创建 `UnityTableEncoder`。

`Query` 主体在 [table_translator.cc:L244-L309](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L244-L309)。第 261–264 行是补全模式下的惰性查询入口：

```cpp
if (enable_completion_) {
  translation = Cached<LazyTableTranslation>(this, code, segment.start,
                                             segment.start + input.length(),
                                             preedit, enable_user_dict);
}
```

`Cached<T>` 是 [translation.h:L116-L118](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L116-L118) 定义的模板，等价于 `New<CacheTranslation>(New<T>(...))`，给候选流加一层缓存装饰器。第 293–300 行是造句兜底与 `sentence_over_completion` 的分支。

`LazyTableTranslation` 在 [table_translator.cc:L117-L207](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L117-L207)。核心是两个常量：`kInitialSearchLimit = 10` 与 `kExpandingFactor = 10`（第 119–120 行）。`FetchMoreTableEntries`（第 189–207 行）先查 10 条，若正好查满 10 条说明可能还有更多，就把 limit ×10 继续查；若没查满说明已到尽头，`limit_ = 0` 停止。这种「按需扩展」避免了一次性把成千上万条补全候选全查出来。

候选的产出在 `TableTranslation::Peek`，见 [table_translator.cc:L73-L94](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L73-L94)。它根据 `remaining_code_length`（还差几个码才完整）和是否用户词，决定候选 `type`（`completion`/`user_table`/`table`），并给不完整候选 quality 减 1（第 90–91 行），让补全候选自然排在精确候选之后。

造句的词图构建在 `MakeSentence`，见 [table_translator.cc:L543-L685](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L543-L685)。第 552–554 行用 `vertices` 集合做 BFS 可达性剪枝：只有从已可达位置出发查到的词，其 end_pos 才会被加入 `vertices`，从而避免无意义的查询。

#### 4.4.4 代码实践

**实践目标**：理解形码查询的「直接码表匹配」本质，对比拼音的「音节图」。

**操作步骤**：

1. 对照 `luna_pinyin.schema.yaml` 第 100–110 行的 `cangjie:` 节，注意 `dictionary: cangjie5`、`tag: cangjie`、`prefix: 'C:'`、`suffix: ';'`。
2. 想象用户输入 `C:abcd;`，recognizer（u6-l2）匹配 `cangjie` 正则后，affix_segmentor（u6-l3）把 `C:` 和 `;` 剥成前后缀段（打 `phony` tag），中间 `abcd` 成 `cangjie` 段。
3. 追踪 `TableTranslator::Query`（[table_translator.cc:L244](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L244)）：第 257–258 行 trim 掉分隔符得到 `code = "abcd"`，第 261 行进入 `LazyTableTranslation`，用 `abcd` 做前缀查 `cangjie5` 码表。

**需要观察的现象**：与 script 不同，table 完全没有「建音节图」这一步；`code` 被当作一个整体字符串直接喂给 `dict_->LookupWords`（见 `FetchMoreTableEntries` 第 196 行）。

**预期结果**：`abcd` 在仓颉码表里命中某个字（如「日」对应 `a`，多码组合对应不同字），补全模式下还会带出以 `abcd` 开头的更长码候选。

> 待本地验证：`cangjie5` 词典不在 `data/minimal` 中，本实践为源码阅读型；若要实跑需自行配置仓颉方案与词典。

#### 4.4.5 小练习与答案

**练习 1**：`LazyTableTranslation` 为什么要「先查 10 条、不够再 ×10」？一次性查完所有候选有什么坏处？

**答案**：形码开启补全时，一个短前缀（如单键 `a`）可能命中成百上千条词条。一次性全查会浪费大量内存与 CPU，而用户通常只看前几页。惰性分批查询保证「显示多少、算多少」，与 Menu 的拉模型（u3-l4）配合，按需扩展。

**练习 2**：候选的 `type` 在 table 里取 `completion`/`user_table`/`table` 三种（[table_translator.cc:L83-L85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L83-L85)），这个 type 有什么下游用途？

**答案**：type 被下游组件用来区分候选来源。例如 `reverse_lookup_translator` 的 `Compare`（见 4.5）会让 `completion` 类型候选排到精确候选之后；`Memorize`（[table_translator.cc:L337-L340](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/table_translator.cc#L337-L340)）在编码提交历史时只接受 `table`/`user_table`/`sentence`/`uniquified` 类型，过滤掉其他。

---

### 4.5 辅助翻译器族

主力翻译器（script/table）负责「把字母变成汉字」，但输入法还有许多「非主流」的候选来源：标点、反查、方案切换、回显、历史复用。这些由辅助翻译器承担，它们大多**不继承 `Memory`/`TranslatorOptions`**，实现更轻。

#### 4.5.1 概念说明

| 翻译器 | 触发条件 | 产出 |
| --- | --- | --- |
| `punct_translator` | 段带 `punct` 或 `punct_number` tag | 标点候选（可成对、可循环切换） |
| `reverse_lookup_translator` | 段带 `reverse_lookup` tag（可配置） | 用词典 A 查码、用反查词典 B 给候选加注释 |
| `echo_translator` | 任何段（不挑 tag） | 把原始输入原样作为一个 `raw` 候选，quality 最低 |
| `history_translator` | 段匹配配置的 `input` 串 | 把最近提交的历史词条作为候选 |
| `schema_list_translator` | 仅在 Switcher 引擎里 | 列出所有可用方案 |
| `switch_translator` | 仅在 Switcher 引擎里 | 列出开关/单选组 |

后两者只在「方案切换器」这个特殊引擎里有效——它们通过 `dynamic_cast<Switcher*>(engine_)` 判断，不是 Switcher 就直接返回 `nullptr`。

#### 4.5.2 核心流程

**reverse_lookup_translator** 是辅助族里最复杂的一个，典型用途是「用拼音反查仓颉」。它持有**两个**词典：`dict_`（用于把输入查成候选文字）和 `rev_dict_`（`ReverseLookupDictionary`，用于把候选文字反查成另一种编码作为注释）。流程：

1. tag 门控（默认 `reverse_lookup`，可由 `tag:` 配置）。
2. 首次调用时懒加载（`Initialize`）：读 `prefix`/`suffix`/`tips`，加载查码词典和反查词典。
3. 从输入中剥掉 `prefix`/`suffix` 得到 `code`。
4. 用 `code` 在 `dict_` 查出候选文字。
5. `Peek` 时用 `rev_dict_->ReverseLookup(文字)` 得到注释（如用拼音查出的字，注释里显示其仓颉码）。

**schema_list_translator / switch_translator** 的 `Query` 几乎只做一件事：`dynamic_cast<Switcher*>` 成功后构造一个 `FifoTranslation`，把方案列表/开关列表填进去。候选本身还混入了 `SwitcherCommand` 接口，被选中时执行「切换方案」或「设置 option」的副作用。

**punct_translator** 根据 `punctuator/half_shape`（或 `full_shape`）映射表把单个标点键翻译成候选；定义可以是单值（`UniqueTranslation`）、列表（循环切换）、`commit`/`pair`（成对）。

**echo_translator** 是兜底：任何没有被其他翻译器认领的输入，都会被它原样作为一个 `raw` 候选，quality 设为 `-100`（最低），保证用户至少能看到自己输入了什么。它的 `Compare` 还会在有其他候选时把自己标记为 exhausted，主动让位。

**history_translator** 当输入串精确等于配置的 `input`（如一个特定按键）时，把最近若干条提交历史作为候选，用于「重复输入上次的内容」。

#### 4.5.3 源码精读

`ReverseLookupTranslator::Query` 在 [src/rime/gear/reverse_lookup_translator.cc:L143-L200](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/reverse_lookup_translator.cc#L143-L200)。第 157–163 行剥 prefix/suffix；第 174–192 行根据是否 `enable_completion` 走两条查询路径（补全用 `LookupWords`，精确用 `SyllableGraph`）。候选注释在 `ReverseLookupTranslation::Peek`（第 56–74 行）里由 `rev_dict_->ReverseLookup` 生成。注意它的构造函数（第 92–101 行）有个小技巧：若 `ticket.name_space == "translator"`，则把 `name_space_` 改成 `"reverse_lookup"`，方便默认配置。

`SchemaListTranslator::Query` 在 [src/rime/gear/schema_list_translator.cc:L131-L138](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/schema_list_translator.cc#L131-L138)，核心是第 133 行的 `dynamic_cast<Switcher*>(engine_)`。`LoadSchemaList`（第 85–126 行）把当前方案放第一个，其余按最近使用时间排序（第 122–125 行的 `stable_sort` by quality，quality 在这里被复用为时间戳）。

`SwitchTranslator::Query` 在 [src/rime/gear/switch_translator.cc:L266-L273](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/switch_translator.cc#L266-L273)，同样靠 `dynamic_cast<Switcher*>` 门控。

`PunctTranslator::Query` 在 [src/rime/gear/punctuator.cc:L325-L360](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/punctuator.cc#L325-L360)，按 `punct`/`punct_number` 分支，再按定义类型（`ConfigValue`/`ConfigList`/`ConfigMap`）派发到四个 `Translate*Punct` 方法。

`EchoTranslator::Query` 在 [src/rime/gear/echo_translator.cc:L30-L43](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/echo_translator.cc#L30-L43)，第 38 行造一个 `raw` 候选，第 40 行 `set_quality(-100)` 压到最低。

`HistoryTranslator::Query` 在 [src/rime/gear/history_translator.cc:L32-L57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/history_translator.cc#L32-L57)，第 36 行要求 `input_ == input`（精确匹配配置串），第 43–55 行倒序遍历 `commit_history` 收集候选。

#### 4.5.4 代码实践

**实践目标**：在 `luna_pinyin.schema.yaml` 里找到 `reverse_lookup_translator` 的完整触发链。

**操作步骤**：

1. 在 `luna_pinyin.schema.yaml` 第 60 行确认 translators 清单含 `reverse_lookup_translator`。
2. 看第 128–137 行的 `reverse_lookup:` 节：`dictionary: cangjie5`（查码词典）、`prefix: '\`'`、`suffix: "'"`、`enable_completion: true`、`comment_format` 把仓颉字母转成字根注释。
3. 看第 159 行 `recognizer/patterns/reverse_lookup: "\`[a-z]*'?$"`——这个正则让 recognizer 在输入以 `` ` `` 开头时给段打 `reverse_lookup` tag（详见 u6-l2/u6-l3）。
4. 追踪 `ReverseLookupTranslator::Query`：第 157 行剥掉 `` ` `` 前缀，第 161 行剥掉 `'` 后缀，第 175 行用中间的字母在 `cangjie5` 词典补全查询，`Peek` 时反查拼音作为注释。

**需要观察的现象**：`reverse_lookup` 节的 `comment_format`（第 137 行 `xform/([nl])v/$1ü/`）与 `cangjie` 节的 `comment_format`（第 109 行字根映射）不同——前者把反查出的拼音 `nv` 显示成 `nü`，后者把仓颉字母显示成字根。同一个 `comment_formatter_` 机制（来自 `TranslatorOptions`）服务于完全不同的展示目的。

**预期结果**：用户输入 `` `ni'hao' `` 时，候选是仓颉码 `ni'hao` 对应的字，注释里显示该字的拼音。这就是「用拼音反查仓颉」的实现。

> 待本地验证：`cangjie5` 词典不在 `data/minimal`；上述为配置与源码阅读型结论。

#### 4.5.5 小练习与答案

**练习 1**：`echo_translator` 为什么要把 quality 设成 `-100`？它和 `fallback_segmentor`（u6-l3）是什么关系？

**答案**：`-100` 让 echo 候选在 `MergedTranslation` 归并时排到最后，只有当其他翻译器都不出候选时它才会被用户看到。它和 `fallback_segmentor` 是「翻译层」与「切分层」的对应：后者把无法识别的输入兜底成 `raw` 段，前者把这个 `raw` 段兜底成一个原样候选，二者配合保证「输入一定有反馈」。

**练习 2**：`schema_list_translator` 和 `switch_translator` 为什么用 `dynamic_cast<Switcher*>(engine_)` 判断，而不是像别的翻译器那样用 tag 门控？

**答案**：因为它们只应出现在「方案切换器」这个特殊引擎里（Switcher 本身也是一种 Engine，见 u9-l4）。普通输入引擎里没有方案列表/开关候选的概念。用 `dynamic_cast` 能让这两个翻译器在普通引擎里直接返回 `nullptr`，无需额外配置 tag；只有当引擎确实是 Switcher 时才生效。

---

## 5. 综合实践

**任务**：解释 `luna_pinyin.schema.yaml` 中 `script_translator` 与 `table_translator@cangjie` 为何能在同一个方案里共存，并画出它们各自的「输入 → tag → 翻译器」链路。

请按以下步骤完成：

1. **定位两个翻译器的配置**：
   - `script_translator`（裸名，第 61 行）：默认 `name_space_ = "translator"`，读第 88–91 行的 `translator:` 节（`dictionary: luna_pinyin`）。
   - `table_translator@cangjie`（第 62 行）：`name_space_ = "cangjie"`，读第 100–110 行的 `cangjie:` 节（`dictionary: cangjie5`，`tag: cangjie`）。

2. **比较二者为何不冲突**：

   | 维度 | `script_translator` | `table_translator@cangjie` |
   | --- | --- | --- |
   | 类 | `ScriptTranslator` | `TableTranslator` |
   | `name_space_` | `translator` | `cangjie` |
   | 认领的 tag | `abc`（默认） | `cangjie`（由 `cangjie/tag` 设定） |
   | 词典 | `luna_pinyin`（拼音） | `cangjie5`（仓颉码表） |
   | 查询方式 | 音节图 `Lookup(syllable_graph)` | 字符串前缀 `LookupWords(code)` |
   | 触发输入 | 普通字母（如 `nihao`） | 带 `C:` 前缀（如 `C:abcd;`） |

   二者认领**不同的 tag**，所以同一段输入只会被其中一个接管：普通字母走 `abc` 段 → `script_translator`；`C:` 开头走 `cangjie` 段 → `table_translator@cangjie`。这就是共存的基础。

3. **画出链路**（文字版时序）：
   - 拼音：`speller` 追加字母 → `abc_segmentor` 打 `abc` tag → `script_translator`（认领 `abc`）建音节图查 `luna_pinyin` 词典。
   - 仓颉：`recognizer` 匹配 `C:[a-z']*;` → `affix_segmentor@cangjie` 剥出 `cangjie` 段 → `table_translator@cangjie`（认领 `cangjie`）用码查 `cangjie5` 词典。

4. **延伸思考**：第 63 行还有 `script_translator@pinyin`（`name_space_ = "pinyin"`，第 112–120 行 `enable_user_dict: false`）。它和第 61 行的 `script_translator` 是**同一个类、不同实例**：前者用于 `P:` 前缀的「纯拼音反查」（不写用户词典），后者是主力拼音。请用本讲学到的「注册一次、实例多次」解释这两个实例如何各自读取 `translator:` 与 `pinyin:` 两个不同配置节。

> 本实践为配置与源码阅读型，无需编译；若要实跑 `P:` 前缀功能，需按 u1-l5 构建并在 console 中输入 `P:ni'hao;` 观察。

## 6. 本讲小结

- **Translator 基类契约**极简：`Query(input, segment) -> an<Translation>`，靠 `segment.HasAnyTagIn(tags_)` 做 tag 门控；`name_space_` 决定从方案的哪一节读配置。
- **共享积木**集中在 `translator_commons`：`TranslatorOptions`（一揽子通用配置）、`Phrase`（`DictEntry` 的薄包装候选）、`Sentence`（多词整句候选）。`ScriptTranslator` 与 `TableTranslator` 都多重继承 `Translator + Memory + TranslatorOptions`。
- **`script_translator`** 面向音码：先建 `SyllableGraph` 把输入切成所有可能的音节序列，再沿图查词典；支持纠错、造句、用户词优先；适合拼音。
- **`table_translator`** 面向形码：把输入当作直接编码做字符串前缀匹配，`LazyTableTranslation` 按需分批扩展；带 `UnityTableEncoder` 为用户自造词生成编码；适合仓颉/五笔。
- **辅助翻译器族**各司其职：`reverse_lookup_translator`（双词典反查）、`punct_translator`（标点）、`echo_translator`（最低优先级兜底）、`history_translator`（历史复用）、`schema_list_translator`/`switch_translator`（仅 Switcher 引擎里列出方案/开关）。
- **数据驱动装配**让一个方案能同时挂多个翻译器实例（如 `script_translator` 与 `script_translator@pinyin`、`table_translator@cangjie`），靠 `@alias` 覆盖 `name_space_` 各读各的配置节，互不冲突的根源是它们认领不同的 tag。

## 7. 下一步学习建议

- 本讲的「音节图」是一个反复出现却一直被当黑盒的概念。建议下一站读 **u7（拼写代数与音节切分）**，尤其是 u7-l3 `Syllabifier`，弄清 `SyllableGraph` 的 vertices/edges/indices 三张表如何由 `Prism` 构建。
- `ScriptTranslator` 与 `TableTranslator` 都重度依赖词典查询。要理解 `dict->Lookup(syllable_graph)` 与 `dict->LookupWords(code)` 背后的存储结构，建议进入 **u8（词典系统）**：u8-l2 `Prism`（双数组 trie 音节索引）、u8-l3 `Table`（多级码表索引）、u8-l5 `Dictionary` 查询主链路。
- 用户词典学习（`Memory::OnCommit` → `Memorize`）涉及 `CommitEntry` 与 `UserDictionary`，详见 **u8-l6 用户词典与 user_db**。
- 若想动手写一个自己的翻译器，可直接跳到 **u9-l6 插件开发实战**，参照 `sample` 插件的 `TrivialTranslator` 写一个返回固定候选的最小 Translator。
