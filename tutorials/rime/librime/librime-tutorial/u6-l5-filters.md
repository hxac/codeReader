# Filter 组件族

## 1. 本讲目标

上一篇 u6-l4 讲完了 **Translator（翻译器）**：它把一段输入翻译成一串候选词。但翻译器产出的候选往往还「半成品」——可能是繁体字而用户想要简体、可能多个翻译器吐出了重复文本、可能混进了扩展 CJK 区的生僻字。本讲要解决的，就是**候选上屏前的最后一道深加工**。

学完本讲，你应当能够：

- 说清 `Filter` 基类的契约：`Apply(translation, candidates)` 如何以「洋葱式」装饰器层层包装候选流。
- 解释 `simplifier` 如何借助 `rime::Opencc` 对候选做繁简/区域字形转换，以及它的开关、提示、多形态展开等行为。
- 解释 `uniquifier` 如何把文本相同的候选合并成一个 `UniquifiedCandidate`。
- 了解 `charset_filter`（按字符集过滤）、`single_char_filter`（单字前置重排）、`reverse_lookup_filter`（给候选补编码注释）这三类辅助过滤器。
- 对照 `luna_pinyin.schema.yaml` 的 `filters` 清单，画出一条候选依次穿过整条过滤链的数据流。

## 2. 前置知识

本讲默认你已经读过：

- **u5-l2 四大组件基类**：知道 `Filter` 是流水线四类组件之一，统一以 `Ticket` 构造、靠方案 `engine/filters` 清单实例化。
- **u3-l3 Composition、Translation 与 Candidate**：知道 `Translation` 是候选流的惰性迭代器（`Peek`/`Next`/`exhausted`），`Candidate` 是候选项数据模型，有 `ShadowCandidate` 等子类。
- **u3-l4 Menu 与分页**：知道 `Menu` 是 Translator 与前端之间的惰性缓冲层，`AddFilter` 把过滤器层层包在 `result_` 外面，并把已确认候选写进一份「共享候选区」`candidates_`。
- **u6-l4 Translator 组件族**：知道翻译器产出的候选会进入 `Menu`。

几个本讲反复用到的术语，先在这里点一下：

- **装饰器（Decorator）模式**：过滤器不直接改原对象，而是把旧的 `Translation` 包进一个新的 `Translation` 子类里，对外暴露同一个 `Peek/Next` 接口。多个过滤器套在一起就像洋葱，一层包一层。
- **拉模型（Pull model）**：候选是「被要的时候才算出来的」。前端读菜单第 N 个候选时，`Menu::Prepare` 才会沿着洋葱从外到内 `Peek`，真正触发转换与过滤。
- **OpenCC**：一个开源的中文转换库，做繁简、地区字形（大陆/台湾/香港）之间的映射，是 `simplifier` 的底层引擎。

## 3. 本讲源码地图

本讲涉及的源码集中在 `src/rime/gear/`，外加两个基础设施文件：

| 文件 | 作用 |
| --- | --- |
| [src/rime/filter.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h) | `Filter` 抽象基类，定义 `Apply`/`AppliesToSegment` 契约。 |
| [src/rime/gear/filter_commons.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/filter_commons.h) / [.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/filter_commons.cc) | `TagMatching` 混入类：让过滤器按 segment 的 tag 决定是否生效。 |
| [src/rime/gear/simplifier.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/simplifier.cc) | 繁简/字形转换过滤器，本讲核心。 |
| [src/rime/gear/opencc.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/opencc.cc) | 对 OpenCC 库的薄封装 `rime::Opencc`，本讲核心。 |
| [src/rime/gear/uniquifier.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/uniquifier.cc) | 同形候选合并过滤器，本讲核心。 |
| [src/rime/gear/charset_filter.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/charset_filter.cc) | 按字符集（扩展 CJK）过滤候选。 |
| [src/rime/gear/single_char_filter.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/single_char_filter.cc) | 把单字候选排到词组前面。 |
| [src/rime/gear/reverse_lookup_filter.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/reverse_lookup_filter.cc) | 用反查词典给候选补一条编码注释。 |
| [src/rime/menu.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc) | `Menu::AddFilter` 把过滤器套上 `result_`。 |
| [src/rime/gear/gears_module.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc) | 把这些过滤器注册进 Registry。 |

## 4. 核心概念与源码讲解

### 4.1 过滤链装配：Filter 基类、洋葱包装与共享候选区

#### 4.1.1 概念说明

`Filter`（过滤器）是流水线四类组件里**最靠后**的一类。它的位置可以这样理解：

```
Processor（改输入）→ Segmentor（切段）→ Translator（出候选）→ Filter（加工候选）→ Menu（分页）→ 前端
```

翻译器把候选塞进 `Menu` 之后，**还没有真正计算每个候选的最终样貌**。过滤器的作用，是在候选被「拉」出来的那一刻，对它做转换、去重、过滤或重排。比如：

- 把繁体候选转成简体（`simplifier`）。
- 把几个文本相同的候选合并成一个（`uniquifier`）。
- 把扩展 CJK 区的生僻字候选剔除（`charset_filter`）。

理解 Filter 的关键有两点：**装饰器包装** 与 **共享候选区**。这两个机制决定了过滤器之间如何协作。

#### 4.1.2 核心流程

过滤链的装配发生在 `Engine::TranslateSegments` 里，对每个 segment：

1. 建一个空 `Menu`，把各翻译器的 `Translation` 用 `AddTranslation` 塞进一个 `MergedTranslation`（k 路归并）。
2. 遍历方案 `engine/filters` 清单里的每个过滤器，若 `AppliesToSegment(segment)` 返回真，就 `menu->AddFilter(filter)`。
3. `AddFilter` 的实现是关键：它把当前候选流 `result_` 整体当作参数，交给过滤器的 `Apply`，再用返回值**替换** `result_`。

也就是说，每加一个过滤器，就是在原来的候选流外面再套一层。若过滤器注册顺序是 `F1, F2, F3`，则最终：

\[
\text{result} = F_3\big(F_2(F_1(\text{Merged}))\big)
\]

最外层是**最后注册**的那个过滤器。当前端读菜单时，`Menu::Prepare` 从最外层 `Peek`，控制流**由外向内**传递；而一个候选的数据是**由内（Merged）产生、向外逐层加工**后，才被写进共享候选区 `candidates_`。

「共享候选区」指 `Menu` 持有的那份 `CandidateList candidates_`，它同时被 `MergedTranslation`（按引用）和各过滤器（按指针）看到。某些过滤器（典型如 `uniquifier`）必须知道「前面已经吐出过哪些候选」才能去重，靠的就是这个共享区。

#### 4.1.3 源码精读

`Filter` 基类契约非常薄，唯一的核心纯虚函数是 `Apply`：

[src/rime/filter.h:22-38](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h#L22-L38) —— 定义 `Filter` 基类：持有 `engine_` 与 `name_space_`，纯虚 `Apply(translation, candidates)` 返回一个新的 `Translation`，`AppliesToSegment` 默认对所有段生效。

`Apply` 的两个参数分别是「上游候选流」和「共享候选区指针」。注意第二个参数是 `CandidateList*`——这正是「共享候选区」的来源。

装配发生在引擎里：

[src/rime/engine.cc:224-228](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L224-L228) —— 遍历 `filters_`，对生效的过滤器调用 `menu->AddFilter(filter.get())`。`filters_` 的顺序就是 YAML `engine/filters` 列表的书写顺序。

`AddFilter` 一行就把「洋葱」搭起来了：

[src/rime/menu.cc:22-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L22-L24) —— `result_ = filter->Apply(result_, &candidates_);`。注意它把 `candidates_` 的地址传进去，于是过滤器能读写这份共享候选区。

而 `Menu::Prepare` 才是真正「拉」候选的地方：

[src/rime/menu.cc:26-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L26-L35) —— 只要已确认候选数还不足且 `result_` 没耗尽，就 `Peek` 一个塞进 `candidates_` 再 `Next`。注意「`Peek` 到空也 `Next`」——某些过滤器会把候选「吞掉」不入列（返回 nullptr），此时仍要继续推进。

还有一类过滤器（`Simplifier`/`CharsetFilter`/`ReverseLookupFilter`）混入了 `TagMatching`，靠 segment 的 tag 决定是否生效：

[src/rime/gear/filter_commons.cc:14-37](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/filter_commons.cc#L14-L37) —— 构造时从方案 `<name_space>/tags` 读 tag 列表；`TagsMatch` 在 `tags_` 为空时匹配任意段（默认全生效），否则要求段至少带其中一个 tag。

#### 4.1.4 代码实践

1. **实践目标**：把「洋葱包装」与「共享候选区」两个机制看明白，画出一张装配图。
2. **操作步骤**：
   - 打开 [src/rime/menu.cc:15-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L15-L24)，确认 `Menu` 构造时 `result_` 指向 `MergedTranslation`，而 `candidates_` 同时被 `MergedTranslation`（构造参数，见 u3-l4）和 `AddFilter` 引用。
   - 打开 [src/rime/engine.cc:203-233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L203-L233)，确认过滤器按 `filters_` 顺序逐个 `AddFilter`。
3. **需要观察的现象**：`AddFilter` 没有循环、没有分支，就是一次赋值——这说明每加一个过滤器，「候选流」这个对象就被换成了「过滤后的候选流」。
4. **预期结果**：你能写出给定 `filters: [A, B, C]` 时最终的 `result_` 表达式为 `C(B(A(Merged)))`，并指出最外层是 `C`。
5. 如果想在运行时验证，可用 `rime_api_console`（见 u1-l5）配合带多个过滤器的方案，候选顺序与去重行为会反映包装层次——具体输出「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：若方案 `filters` 写成 `[simplifier@zh_simp, uniquifier]`，哪个过滤器在最外层？候选数据先经过谁？

**答案**：`uniquifier` 在最外层（它后注册）。但候选**数据**是先被 `simplifier` 转换、再被 `uniquifier` 去重的——因为数据由最内层产生、向外逐层冒泡。

**练习 2**：为什么 `Apply` 的第二个参数是 `CandidateList*` 而不是值传递？

**答案**：因为像 `uniquifier` 这样的过滤器需要读写「Menu 已经确认的候选区」来做跨候选的去重判断，必须共享同一份列表。

**练习 3**：`AppliesToSegment` 默认返回 `true`，意味着什么？

**答案**：意味着该过滤器对该方案里**所有** segment 都生效；若只想作用于特定段，需混入 `TagMatching` 并在配置里写 `tags: [...]`。

### 4.2 Simplifier 与 Opencc：繁简/区域字形转换

#### 4.2.1 概念说明

`simplifier` 是最常用的过滤器。它的职责是：把翻译器吐出的候选（通常是某种「正字」形态，比如繁体）转换成用户想要的字形——简体、台湾字形、香港字形等。底层转换交给 `rime::Opencc`，这是 librime 对 [OpenCC](https://github.com/BYVoid/OpenCC) 库的一层薄封装。

几个关键设计点：

- **开关驱动**：转换不是无脑做的，而是由一个开关 option 控制（默认叫 `simplification`，可配置）。开关关掉时，过滤器直接「透传」，候选原样穿过。
- **一词多形**：一个字可能有多种转换结果（比如「里」在简繁转换里既能是「裏」也能是「里」「哩」）。`simplifier` 会把每一种形态都展开成一条候选。
- **提示（tips）**：转换后可以保留原始字形作为提示，显示在候选的注释位（comment）上，让用户知道这条候选是从哪个字转来的。
- **配置复用**：多个 `simplifier@xxx` 实例若用同一份 OpenCC 配置文件，会共享同一个 `Opencc` 对象（靠 `weak_ptr` 缓存）。

#### 4.2.2 核心流程

`simplifier` 的运行分两个阶段：

**装配期（`SimplifierComponent::Create`）**：

1. 从方案读 `<name_space>/opencc_config`（默认 `t2s.json`，即繁→简）。
2. 在 `opencc_map_` 里按配置文件名查缓存（`weak_ptr::lock`）；命中就复用。
3. 未命中则解析路径（相对路径落在 `user_data_dir/opencc` 或 `shared_data_dir/opencc` 下），`new Opencc(path)`，存进缓存。
4. 用这个 `Opencc` 实例构造 `Simplifier`。

**运行期（每次拉取候选）**：

1. `Apply` 先看开关 `option_name_` 是否打开；没开就返回原 `translation`（透传）。
2. 开关开着，就返回一个 `SimplifiedTranslation`（`PrefetchTranslation` 的子类）包住上游。
3. `SimplifiedTranslation::Replenish` 每次从上游 `Peek` 一个候选，交给 `Simplifier::Convert` 转换。
4. `Convert` 的逻辑：
   - 若候选 `type` 在 `excluded_types_` 黑名单里，跳过（返回 false，原候选透传）。
   - 否则先试 `ConvertWord`（按「词」精确匹配，可能返回多种形态）；失败再退回 `ConvertText`（整段文本转换，单一结果）。
   - 对每种结果：若与原文相同，直接保留原候选（保住它的 type 与 quality）；若不同，包成一个 `ShadowCandidate`（type 标为 `"simplified"`）。
5. `Convert` 返回 false（没有任何转换）时，原候选原样入列。

#### 4.2.3 源码精读

先看过滤器主体。构造期读取一大堆配置项（tips 等级、是否在注释位显示、是否继承原注释、comment_format、是否随机、开关名、排除类型）：

[src/rime/gear/simplifier.cc:29-62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/simplifier.cc#L29-L62) —— `Simplifier` 构造函数。注意 `option_name_` 默认兜底为 `"simplification"`（第 56-58 行）。

`Apply` 是开关与透传的枢纽：

[src/rime/gear/simplifier.cc:84-93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/simplifier.cc#L84-L93) —— 开关关或 `opencc_` 为空时直接 `return translation`；否则包成 `SimplifiedTranslation`。

`SimplifiedTranslation` 继承 `PrefetchTranslation`——这是一种「先把上游候选拉进来、转换后填进本地 `cache_` 队列、再逐个吐出」的模式：

[src/rime/gear/simplifier.cc:64-82](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/simplifier.cc#L64-L82) —— `Replenish`：从上游 Peek 一个、Next 推进，调用 `Convert`；若转换失败（`!Convert`），就把原候选塞进 `cache_`。这正是「转不动就透传」的实现。

转换核心 `Convert`，体现「一词多形 + 退化策略」：

[src/rime/gear/simplifier.cc:125-157](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/simplifier.cc#L125-L157) —— 先排除黑名单类型；非随机模式下先 `ConvertWord`（多个 `forms`），每个 form 与原文相同则留原候选、不同则 `PushBack` 一个 `ShadowCandidate`；`ConvertWord` 失败再退回 `ConvertText`。

`PushBack` 决定候选最终长什么样（文字、提示、注释）：

[src/rime/gear/simplifier.cc:95-123](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/simplifier.cc#L95-L123) —— 根据 `tips_level_`（`kTipsNone/kTipsChar/kTipsAll`）和 `show_in_comment_` 决定文字与提示。提示默认用 `quote_left`/`quote_right`（即 `〔〕`，见文件顶部第 22-23 行的 UTF-8 字节）把原字包起来，最终 `New<ShadowCandidate>(original, "simplified", text, tips, inherit_comment_)`。

再看底层 `Opencc`。它是惰性初始化的——构造只存路径，第一次转换时才真正加载 OpenCC 字典：

[src/rime/gear/opencc.cc:17-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/opencc.cc#L17-L35) —— `Opencc::Opencc` 只记 `config_path_`；`Initialize()` 用 `opencc::Config::NewFromFile` 解析 JSON 配置、取出转换链 `converter_` 与首个字典 `dict_`，异常被 `catch(...)` 吞掉并记 ERROR（`converter_` 保持 null）。

`ConvertWord` 是最复杂的一个——它要沿着「转换链」逐个字典走，且每个字典可能把一个词展开成多个候选：

[src/rime/gear/opencc.cc:37-100](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/opencc.cc#L37-L100) —— 对链中每个字典：精确 `Match` 命中则展开它的 `Values()`（多形态）；未命中则用 `MatchPrefix` 做最长前缀切分（逐字符兜底）。结果在字典间累积（`original_words.swap(converted_words)`）。若整条链没有任何字典命中，返回 false。

`ConvertText` 则简单得多，直接用 `converter_->Convert` 做整段转换：

[src/rime/gear/opencc.cc:135-141](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/opencc.cc#L135-L141) —— 整段转换；若结果与原文相同返回 false（避免无意义的 ShadowCandidate）。

最后看组件工厂如何缓存 `Opencc` 实例：

[src/rime/gear/simplifier.cc:161-198](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/simplifier.cc#L161-L198) —— `SimplifierComponent::Create`：读 `opencc_config`（默认 `t2s.json`），用 `opencc_map_[config].lock()` 查 `weak_ptr` 缓存；未命中则定位文件（相对路径优先 `user_data_dir/opencc`，其次 `shared_data_dir/opencc`）、`New<Opencc>` 并以**原始配置串**为 key 存缓存。遇到旧的 `.ini` 配置会拒绝并报错。

#### 4.2.4 代码实践

1. **实践目标**：用项目自带的测试理解 `Opencc` 的「一词多形」与「转换链累积」行为。
2. **操作步骤**：
   - 阅读 [test/simplifier_test.cc:28-66](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/simplifier_test.cc#L28-L66)，这几条 `OpenccTest` 用真实的 OpenCC 字典文件验证：`裡→里`（单形态）、`里→{裏,里,哩}`（多形态）、`s2twp` 链式转换后「裏」被「裡」替换。
   - 再看 [test/simplifier_test.cc:208-253](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/simplifier_test.cc#L208-L253)，`SimplifierConvertTest` 用一个 `FakeOpencc`（不依赖真实字典）验证 `Simplifier::Convert` 的三种结果：转成 ShadowCandidate、原候选直推、退化到 `ConvertText`、全部失败返回 false。
   - 若本地已构建（`BUILD_TEST=ON`，见 u1-l2），可运行 `ctest -R Opencc` 或直接跑 `simplifier_test` 观察输出。
3. **需要观察的现象**：当 `ConvertWord` 返回多个形态、且其中一个等于原文时，原候选（保留原 type `"word"`）会排在前面，转换后的 ShadowCandidate（type `"simplified"`）排在后面。
4. **预期结果**：与 `ConvertWord_UnchangedFormPushesOriginalCandidate` 测试断言一致——两条候选，第一条 type 为 `"word"` 文本「裡」，第二条 type 为 `"simplified"` 文本「里」。
5. 真实运行结果「待本地验证」（依赖 OpenCC 字典目录 `RIME_OPENCC_DICT_DIR` 是否就绪）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `simplifier` 默认开关名是 `simplification`，而 `luna_pinyin` 里却用 `zh_simp`/`zh_tw`？

**答案**：因为方案在 `zh_simp`/`zh_tw` 配置节里显式写了 `option_name: zh_simp`（见 [data/minimal/luna_pinyin.schema.yaml:139-145](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L139-L145)），覆盖了默认值。这样多个 simplifier 可以绑定到不同的开关，实现「简/繁/港/台」单选切换。

**练习 2**：`ConvertWord` 与 `ConvertText` 有何区别？为什么 `Convert` 要先试前者？

**答案**：`ConvertWord` 按「词」精确匹配并展开所有形态（一词多形），适合命名实体等整词转换；`ConvertText` 做整段文本转换、只给单一结果。先试 `ConvertWord` 是为了在有词级词条时拿到更准确的转换（比如 `内存→記憶體`），拿不到再退化到逐字转换。

**练习 3**：若 OpenCC 配置文件路径不存在，会发生什么？

**答案**：`Initialize()` 里 `config.NewFromFile` 抛异常被 `catch(...)` 吞掉，`converter_` 保持 null，所有 `Convert*` 返回 false（见 [test/simplifier_test.cc:147-156](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/simplifier_test.cc#L147-L156) 的 `Initialize_InvalidConfigPath`）；于是 `Convert` 返回 false、原候选透传，不会崩溃。

### 4.3 Uniquifier：同形候选合并

#### 4.3.1 概念说明

`uniquifier`（去重器）解决一个很现实的问题：当一条过滤链里挂了多个 `simplifier`、或者多个翻译器都吐出了文本相同的候选时，候选菜单里会出现**文字一模一样**的重复条目。这既浪费版面，也让用户困惑。

`uniquifier` 的做法是：**遇到文本相同的新候选，不单独入列，而是把它「挂」到之前那条同文本候选底下**，合并成一个 `UniquifiedCandidate`。对前端而言，菜单里仍然只显示一条，但这条候选背后其实「叠」了好几个来源（质量取最大值）。

它必须放在过滤链的**最后**（最外层）才有意义——因为只有当所有转换都做完之后，「最终文本」才稳定，才能判断是否重复。

#### 4.3.2 核心流程

`Uniquifier::Apply` 返回一个 `UniquifiedTranslation`（`CacheTranslation` 的子类），它持有指向共享候选区 `candidates_` 的指针。核心逻辑在 `Uniquify()`：

1. 循环 `Peek` 上游候选 `next`。
2. 在共享候选区 `candidates_` 里线性查找有没有文本和 `next` 相同的候选（`find_text_match`）。
3. **没找到**（唯一）：返回 true，把 `next` 作为一条独立候选留在流里，等 `Menu::Prepare` 把它收进 `candidates_`。
4. **找到了**（重复）：把之前那条候选「升级」成 `UniquifiedCandidate`（若还不是），再把 `next` `Append` 进去，然后跳过它（`CacheTranslation::Next`），继续看下一个。
5. 直到上游耗尽。

`UniquifiedCandidate`（定义在 [src/rime/candidate.h:112-146](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L112-L146)）内部持有一个 `CandidateList items_`，`text()`/`comment()`/`preedit()` 都取 `items_.front()`，但 `Append` 时会把 quality 提升到所有 item 的最大值。

#### 4.3.3 源码精读

`Uniquifier::Apply` 极简，只是把共享候选区指针透传给 `UniquifiedTranslation`：

[src/rime/gear/uniquifier.cc:66-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/uniquifier.cc#L66-L71) —— 注意它把 `candidates`（即 `Menu` 的共享区）传进了新构造的 `UniquifiedTranslation`。

去重核心：

[src/rime/gear/uniquifier.cc:44-62](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/uniquifier.cc#L44-L62) —— `Uniquify()`：在 `candidates_` 里找相同文本；找不到就 `return true`（保留为独立候选）；找到就把目标升级成 `UniquifiedCandidate` 并 `Append(next)`，然后 `CacheTranslation::Next()` 跳过这个重复候选。

辅助的线性查找：

[src/rime/gear/uniquifier.cc:33-42](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/uniquifier.cc#L33-L42) —— `find_text_match` 按 `text()` 逐项比较，返回已存在候选的迭代器或 `end`。

`Next` 在推进时再次去重，保证流里始终是个「无重复」的状态：

[src/rime/gear/uniquifier.cc:29-31](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/uniquifier.cc#L29-L31) —— `Next()` 调基类的 `Next` 后再 `Uniquify()`，连续跳过所有重复项。

被合并目标的数据结构：

[src/rime/candidate.h:112-146](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L112-L146) —— `UniquifiedCandidate`：构造时 `Append` 第一项；`text()`/`comment()` 取 `items_.front()`；`Append` 把新项 push 进 `items_` 并把 `quality` 提到 `max`。

#### 4.3.4 代码实践

1. **实践目标**：理解「为什么 `uniquifier` 必须放在过滤链最后」以及它对共享候选区的依赖。
2. **操作步骤**：
   - 打开 [data/minimal/luna_pinyin.schema.yaml:64-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L64-L68)，确认 `uniquifier` 是 `filters` 列表的最后一项。
   - 假想一个场景：`simplifier@zh_simp` 把繁体「電腦」转成「电脑」，而 `script_translator` 本身也有一条「电脑」候选。追踪这条「电脑」会如何流过 `uniquifier`。
   - 静态推演：第一条「电脑」进 `candidates_` 时无重复→独立入列；第二条「电脑」到来时 `find_text_match` 命中→被 `Append` 到第一条底下→用户只看到一条。
3. **需要观察的现象**：菜单里不会出现两条文字相同的候选；但被合并的那条候选质量可能比单独看每条都高（取了 max）。
4. **预期结果**：重复候选被合并，前端候选数减少。
5. 实际运行输出「待本地验证」（需构造能稳定产生重复候选的输入）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `uniquifier` 放在 `simplifier@zh_simp` **之前**（即更内层），还能正确去重吗？

**答案**：不能。去重发生在 `simplifier` 转换之前，那时候选文本还是繁体；等 `simplifier` 把它们都转成简体后，又可能产生新的重复，而这些重复 `uniquifier` 已经管不到了。所以 `uniquifier` 必须在最外层（最后做）。

**练习 2**：`UniquifiedCandidate` 合并多条候选后，显示的文字是哪条的？

**答案**：是 `items_.front()` 的文字——即最先入列的那条（见 [src/rime/candidate.h:124-126](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.h#L124-L126)）。其余 item 只是「挂」在底下，影响 quality 但不改显示文本。

**练习 3**：`uniquifier` 为什么需要 `CandidateList* candidates` 这个共享指针，而不能只看自己流内部的候选？

**答案**：因为去重要判断「这条候选是否**已经被 Menu 收进候选区**」，而候选区是 `Menu` 持有的、跨所有过滤器共享的。只看自己流内部无法知道前面已经吐出过什么。

### 4.4 字符集、单字与反查三类辅助 Filter

本节把另外三个常用但较简短的过滤器放在一起讲。它们的共同点是：都遵循 `Filter` 基类契约，但各自只做一件小事。

#### 4.4.1 概念说明

- **`charset_filter`（字符集过滤器）**：剔除「扩展 CJK 区」的生僻字候选（CJK Extension A/B/…/J 及兼容表意文字）。这类字大多字体显示不出来，默认不希望出现。它由开关 `extended_charset` 控制：开关关时过滤、开关开时放行。注册时还有一个别名 `cjk_minifier`（见 [src/rime/gear/gears_module.cc:82-85](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/gears_module.cc#L82-L85)）。
- **`single_char_filter`（单字前置）**：把候选流里的「单字」排到「词组」前面。常用于某些只想要单字的场景（比如人名输入）。它**不过滤**、只**重排**。
- **`reverse_lookup_filter`（反查补码）**：用一份反查词典查出候选对应的编码，写到候选的注释位。典型用途：在拼音方案里，给候选补上仓颉编码作注释，方便学习形码。

#### 4.4.2 核心流程

**`charset_filter`**：`Apply` 时若 `name_space_` 为空且 `extended_charset` 关，就返回 `CharsetFilterTranslation` 包装；否则透传。`CharsetFilterTranslation` 在构造和每次 `Next` 时调用 `LocateNextCandidate`，循环 `Peek`、对每个候选用 `FilterText` 判断（文本不含扩展 CJK 才接受），不接受就 `Next` 跳过，直到找到合格的或耗尽。

**`single_char_filter`**：`Apply` 返回 `SingleCharFirstTranslation`（`PrefetchTranslation` 子类）。构造时 `Rearrange`：把上游候选全拉进来，按「是否单字（UTF-8 字符数为 1）」分成 `top`/`bottom` 两堆，但**只处理** genuine candidate 是 `Phrase` 且 type 为 `"table"`/`"user_table"` 的候选（即码表翻译器的产物）；先把 `top`（单字）拼回 `cache_`，再把 `bottom`（词组）拼回。

**`reverse_lookup_filter`**：`Apply` 懒 `Initialize`（加载 `reverse_lookup_dictionary` 组件与 `overwrite_comment`/`append_comment`/`comment_format` 配置），返回 `ReverseLookupFilterTranslation`（`CacheTranslation` 子类）。它的 `Peek` 被重写：每次被 Peek 时调 `filter_->Process(cand)`——查出编码、套用 `comment_format`、按覆写或追加方式写进候选的 comment。

#### 4.4.3 源码精读

**字符集判断**（哪些码点算「扩展 CJK」）：

[src/rime/gear/charset_filter.cc:18-50](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/charset_filter.cc#L18-L50) —— `is_extended_cjk` 用一串码点区间判断（Extension A `0x3400-0x4DBF`、Extension B `0x20000-0x2A6DF`、…… 一直到 Extension J 与兼容表意文字）；`contains_extended_cjk` 逐 UTF-8 字符扫描。

**`CharsetFilter::Apply`** 的开关逻辑：

[src/rime/gear/charset_filter.cc:101-111](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/charset_filter.cc#L101-L111) —— `name_space_` 空 且 `extended_charset` 关 → 过滤；`extended_charset` 开 → 透传；若设了 `name_space_`（想指定别的字符集）会报 ERROR 并透传（基础实现不支持）。

**`single_char_filter` 的重排**：

[src/rime/gear/single_char_filter.cc:33-56](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/single_char_filter.cc#L33-L56) —— `Rearrange`：用 `unistrlen`（UTF-8 字符数）判单字；用 `As<Phrase>(Candidate::GetGenuineCandidate(cand))` 解出真正的 Phrase，要求其 `type()` 是 `"table"` 或 `"user_table"`；分成 `top`/`bottom` 后依次 splice 回 `cache_`。

**`reverse_lookup_filter` 的补码**：

[src/rime/gear/reverse_lookup_filter.cc:72-89](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/reverse_lookup_filter.cc#L72-L89) —— `Process`：候选已有 comment 且既不覆写也不追加→跳过；否则解出 Phrase，`rev_dict_->ReverseLookup(text, &codes)` 取编码，套 `comment_formatter_`，按覆写（`set_comment(codes)`）或追加（`comment + " " + codes`）写入。

`Apply` 与懒初始化：

[src/rime/gear/reverse_lookup_filter.cc:43-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/reverse_lookup_filter.cc#L43-L70) —— `Initialize` 用 `ReverseLookupDictionary::Require("reverse_lookup_dictionary")` 取组件、`Load()` 加载；若加载失败则 `rev_dict_.reset()`，之后 `Apply` 直接透传（不补码）。

#### 4.4.4 代码实践

1. **实践目标**：搞清三个过滤器各自「改的是什么」——`charset_filter` 删候选、`single_char_filter` 调顺序、`reverse_lookup_filter` 改注释。
2. **操作步骤**：
   - 对照 [data/minimal/luna_pinyin.schema.yaml:122-126](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L122-L126) 的 `cangjie_lookup` 段：`tags: [pinyin]` 表示这个 `reverse_lookup_filter@cangjie_lookup` 只对带 `pinyin` tag 的段（即 `P:...` 输入）生效。
   - 推演：用户输入 `P:ni hao`，segmentor 打 `pinyin` tag（见 u6-l3），`script_translator@pinyin` 译出繁体候选「你好」，`reverse_lookup_filter@cangjie_lookup` 查 cangjie5 词典给它在 comment 位补上仓颉编码。
   - 静态确认 `single_char_filter` 与 `charset_filter` 在 `luna_pinyin` 默认 `filters` 里**并未**启用——它们只是可选组件。
3. **需要观察的现象**：`charset_filter` 生效时扩展 CJK 生僻字消失；`single_char_filter` 生效时单字排到词组前；`reverse_lookup_filter` 生效时候选注释位多出编码。
4. **预期结果**：与上述各 `Apply`/`Process` 的分支一致。
5. 具体运行输出「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`charset_filter` 和 `single_char_filter` 一个删候选、一个调顺序，为什么后者不算「过滤」？

**答案**：`single_char_filter` 保留了**所有**候选，只是把单字挪到词组前面（`top` 在前 `bottom` 在后），候选总数不变；`charset_filter` 则是把扩展 CJK 候选**丢弃**（`LocateNextCandidate` 跳过），候选数减少。

**练习 2**：`reverse_lookup_filter@cangjie_lookup` 为什么只对 `pinyin` 段生效？

**答案**：因为 `cangjie_lookup` 配置里写了 `tags: [pinyin]`，`TagMatching` 构造时读入这个列表（见 [src/rime/gear/filter_commons.cc:14-25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/filter_commons.cc#L14-L25)），`AppliesToSegment` 调 `TagsMatch` 只匹配带 `pinyin` tag 的段。

**练习 3**：`reverse_lookup_filter` 只对什么样的候选能补码？

**答案**：只对 genuine candidate 是 `Phrase` 的候选（[src/rime/gear/reverse_lookup_filter.cc:75-77](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/reverse_lookup_filter.cc#L75-L77)），即 `table_translator`/`script_translator` 这类码表翻译器的产物；普通 `SimpleCandidate`（如标点）不会被补码。

## 5. 综合实践

把本讲所有知识串起来，跟踪 `luna_pinyin` 方案里一条候选穿过完整过滤链的过程。

**方案配置**（[data/minimal/luna_pinyin.schema.yaml:64-68](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L64-L68)）：

```yaml
filters:
  - reverse_lookup_filter@cangjie_lookup   # ① 最内层
  - simplifier@zh_simp                      # ②
  - simplifier@zh_tw                        # ③
  - uniquifier                              # ④ 最外层
```

**任务**：

1. **画出洋葱结构**。按 4.1 的规则，`AddFilter` 每次把 `result_` 替换为 `Apply(result_)`，所以最终：

   \[
   \text{result} = \text{uniquifier}\big(\text{zh\_tw}\big(\text{zh\_simp}\big(\text{cangjie\_lookup}(\text{Merged})\big)\big)\big)
   \]

   最外层是 `uniquifier`，最内层（紧贴 `MergedTranslation`）是 `reverse_lookup_filter@cangjie_lookup`。

2. **推演一条候选的数据流**。假设用户在普通拼音段（非 `P:` 输入）键入 `nihao`，`script_translator` 产出繁体候选「你好」：

   - `MergedTranslation` 吐出「你好」（type 为翻译器产物）。
   - ① `reverse_lookup_filter@cangjie_lookup`：因为该段没有 `pinyin` tag（普通 abc 段），`AppliesToSegment` 返回 false，**不生效**，候选原样穿过。
   - ② `simplifier@zh_simp`：检查开关 `zh_simp`。若用户选了「简化字」，开关开 → 把「你好」转成简体「你好」（此例字形恰好不变，可能走 `ConvertText` 返回 false 而透传，或返回同形直接保留原候选）。若开关关 → 透传。
   - ③ `simplifier@zh_tw`：检查开关 `zh_tw`。由于 `switches` 里 `zh_trad/zh_simp/zh_hk/zh_tw` 是**单选组**（[data/minimal/luna_pinyin.schema.yaml:28-34](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/data/minimal/luna_pinyin.schema.yaml#L28-L34)），任意时刻最多一个开，所以 ② 与 ③ 不会同时转换。
   - ④ `uniquifier`：把可能的重复（比如同一字形的简繁都出现）合并成 `UniquifiedCandidate`，写进共享候选区。

3. **动手验证（源码阅读型 + 可选运行）**：
   - 用 `rime_api_console`（见 u1-l5）加载 `luna_pinyin`，输入一段拼音，用 `set_option zh_simp true`/`set_option zh_tw true` 切换开关，观察候选文本在繁/简/台字形之间的变化。
   - 再用 `P:ni hao`（带 `P:` 前缀）触发 `pinyin` 段，观察候选注释位是否出现仓颉编码（`reverse_lookup_filter@cangjie_lookup` 生效的证据）。
   - 若无法运行，至少完成静态推演：写下一组 `(开关状态, 输入)` 并预测候选文本与注释，再对照源码分支验证你的预测。

4. **思考题**：为什么这条链要把 `uniquifier` 放最后、把 `reverse_lookup_filter` 放最前？

   **参考思路**：`reverse_lookup_filter` 补的是**编码注释**，与字形转换无关，放最内层先补好；两个 `simplifier` 做字形转换，可能产生重复；`uniquifier` 必须等所有转换完成后再去重，所以放最外层。

## 6. 本讲小结

- `Filter` 基类唯一核心是 `Apply(translation, candidates)`：返回一个新的 `Translation` 包住上游；`Menu::AddFilter` 用 `result_ = filter->Apply(result_, &candidates_)` 把过滤器层层套成「洋葱」，**最后注册的最外层**，候选数据由内向外逐层加工。
- `Menu` 的共享候选区 `candidates_` 被同时传给每个过滤器，是跨候选去重（`uniquifier`）的基础。
- `simplifier` 借助 `rime::Opencc` 做繁简/区域字形转换：开关驱动（默认 `simplification`）、`ConvertWord` 一词多形、失败退化到 `ConvertText`、转不动则透传；多个实例共享同一份 OpenCC 字典（`weak_ptr` 缓存）。
- `Opencc` 是 OpenCC 库的薄封装，惰性初始化，沿转换链逐字典累积多形态结果；配置文件路径不存在时安全降级（返回 false，不崩溃）。
- `uniquifier` 把文本相同的候选合并成 `UniquifiedCandidate`，依赖共享候选区做线性查找，必须放在过滤链最后。
- `charset_filter`（开关 `extended_charset`）剔除扩展 CJK 生僻字；`single_char_filter` 把码表单字候选前置（只重排不过滤）；`reverse_lookup_filter` 给码表候选补编码注释、按 tag 生效。

## 7. 下一步学习建议

- **向下深入词典**：`reverse_lookup_filter` 依赖的反查词典在 u8-l1（词典系统总览）与 u8-l5（Dictionary 查询主链路）讲解；`Opencc` 的配置文件（`t2s.json`/`s2twp.json` 等）由 OpenCC 子模块提供，可结合 [src/rime/gear/opencc.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/opencc.cc) 与 OpenCC 文档对照阅读。
- **横向扩展**：若想自己写一个过滤器（比如「过滤掉包含某关键字的所有候选」），可参照 u9-l6（插件开发实战），用 `RIME_REGISTER_MODULE` 注册一个继承 `Filter` 的类，在方案 `engine/filters` 里引用它即可。
- **回顾数据流**：建议回头重读 u3-l4（Menu 与分页）的 `Prepare`/`CreatePage`，把「过滤器洋葱 + 共享候选区 + 分页」三件事在脑子里合成一张完整时序图，至此「一次按键如何变成候选词」的主线（u6 全单元）就闭环了。
