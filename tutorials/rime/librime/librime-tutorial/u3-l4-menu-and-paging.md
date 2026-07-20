# Menu 与分页

> 承接 [u3-l3](u3-l3-composition-translation-candidate.md)：上一篇已经讲清了 `Candidate`、`Translation`、`Filter` 三个数据模型，并提到「Menu 默认用 `MergedTranslation` 归并各翻译器流、再用 Filter 串成链，按 `Prepare` 按需拉取候选」。本篇就把当时被当作「装配者」一笔带过的 `Menu` 类本身彻底拆开，聚焦它的惰性求值机制与分页产出。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `Menu` 在「候选生成」与「前端显示」之间扮演的惰性缓冲层角色；
- 解释 `AddTranslation` / `AddFilter` 如何把若干 `Translation` 和若干 `Filter` 装配成一条「按需求值」的洋葱式流水线；
- 逐步追踪 `Prepare(count)` 是如何从 `Translation` 链中一个一个把候选拉进 `candidates_` 数组的；
- 读懂 `CreatePage(page_size, page_no)` 的边界处理、`is_last_page` 的判定，以及高亮索引 `highlighted_candidate_index` 的计算公式。

## 2. 前置知识

本讲默认你已掌握上一篇（u3-l3）引入的概念，这里只做最简回顾：

- **Candidate（候选）**：一条可上屏的候选项，关键字段是 `text()`（上屏文字）、`start/end`（在输入串中对应的区间）、`quality()`（质量分）。排序规则由 [candidate.cc:34-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/candidate.cc#L34-L49) 的 `compare` 定义：**段首越靠前越优先 → 区间越长越优先 → 质量越高越优先**。
- **Translation（翻译流）**：候选的惰性迭代器，三个核心方法 `Next()` / `Peek()` / `exhausted()`，只有被 `Peek` 的候选才会真正被计算（拉模型）。
- **Filter（过滤器）**：装饰器，`Apply(translation, candidates)` 接收一条 `Translation`、返回一条**新的** `Translation`，常用于在候选流出前做繁简转换、去重等加工。
- **`Segment::menu`**：每个分段（Segment）挂一个 `Menu`，候选就藏在里面（见 [segmentation.h:30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/segmentation.h#L30)）。

两个本讲要强调的设计术语：

- **拉模型（lazy / pull）**：候选不是一次算完，而是「要几个算几个」，要第 N 个时才计算到第 N 个。
- **共享候选区**：`Menu::candidates_` 这份「已经确定下来的候选数组」会被 `MergedTranslation`（按引用）和各个 `Filter`（按指针）**共同读写**。这是本讲最关键的一个设计点，稍后详解。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
|---|---|---|
| `src/rime/menu.h` | `Menu` 与 `Page` 的类声明 | 字段与接口全貌 |
| `src/rime/menu.cc` | `Menu` 的全部实现（**本讲核心**） | 构造、`AddTranslation`/`AddFilter`/`Prepare`/`CreatePage` |
| `src/rime/translation.cc` | `MergedTranslation` 的 k 路归并实现 | `Elect` 选举逻辑 |
| `src/rime/filter.h` | `Filter::Apply` 装饰器接口 | 理解 `AddFilter` 的链式包装 |
| `src/rime/engine.cc` | `TranslateSegments` 装配 `Menu` | 流水线如何造出 `Menu` |
| `src/rime_api_impl.h` | C API 用 `CreatePage` 把候选交给前端 | 分页与高亮索引的真正消费者 |
| `src/rime/schema.cc` | `page_size` 配置项来源 | 默认 5 |

## 4. 核心概念与源码讲解

### 4.1 Menu：候选流的惰性包装器

#### 4.1.1 概念说明

`Menu` 处在「候选生成（Translator）」与「前端显示」之间，是一个**惰性缓冲层**。引擎每翻译一个 `Segment`，就 `new` 一个 `Menu`，把所有 Translator 的输出喂进去，再用 Filter 链层层包装。`Menu` 内部并不立即算出全部候选，而是维护两样东西：

1. 一条**还没消费完的候选流** `result_`（一条 `Translation`，可能已经被多层 Filter 包裹）；
2. 一份**已经确定下来**的候选数组 `candidates_`。

前端要第 N 个候选时，`Menu` 才从 `result_` 里逐个拉到第 N 个，存进 `candidates_`。

为什么需要这个缓冲层？拼音词典里一个音节可能对应成百上千条词条，但屏幕一页只显示 `page_size`（默认 5）个候选。一次性把所有候选都求值出来，既慢又费内存。惰性求值让「显示多少、算多少」成为可能。

最关键的设计点是**共享候选区**：`candidates_` 不是 `Menu` 私有的，它被 `MergedTranslation`（通过 `const` 引用 `previous_candidates_`）和每个 `Filter`（通过 `CandidateList*` 指针）共享访问。这样一来，当 `Prepare` 把新候选 `push_back` 进 `candidates_` 时，下游的去重类 Filter（如 `uniquifier`）就能立刻「看见」已经吐出去的候选，从而避免重复上屏。这种「边拉取、边共享」的协作是整个 Menu 机制的灵魂。

#### 4.1.2 核心流程

装配阶段（由 `Engine::TranslateSegments` 驱动）：

```
new Menu()                                    // 造一个空 Menu
  -> merged_ = new MergedTranslation(candidates_)   // 归并器持有 candidates_ 引用
  -> result_ = merged_                              // 链头先指向归并器

for 每个 translator:
    menu->AddTranslation(translation)         // 塞进归并器
for 每个 filter（若 AppliesToSegment 为真）:
    menu->AddFilter(filter)                   // result_ = filter->Apply(result_, &candidates_)
```

消费阶段（由前端 / Selector 驱动）：

```
Prepare(n):
    while (candidates_.size() < n 且 result_ 未耗尽):
        c = result_->Peek()                   // 可能被 Filter 吞掉而返回空
        if (c) candidates_.push_back(c)
        result_->Next()                       // 无论是否拿到，都推进游标

CreatePage(page_size, page_no):
    start = page_size * page_no
    end   = min(start + page_size, 实际能拉到的数量)
    把 candidates_[start, end) 拷成一个 Page 返回
```

#### 4.1.3 源码精读

先看 `Page` 这个返回给调用方的值类型，它就是「一页候选」的快照：[src/rime/menu.h:16-21](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.h#L16-L21)。

```cpp
struct Page {
  int page_size = 0;          // 每页容量
  int page_no = 0;            // 第几页（从 0 起）
  bool is_last_page = false;  // 是否最后一页
  CandidateList candidates;   // 本页的候选
};
```

再看 `Menu` 的私有成员，三个字段一目了然：[src/rime/menu.h:44-48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.h#L44-L48)。

```cpp
 private:
  an<MergedTranslation> merged_;   // k 路归并器
  an<Translation> result_;         // 当前候选流链头（被 Filter 层层包裹）
  CandidateList candidates_;       // 已确定下来的候选（被共享读写）
```

构造函数把 `candidates_` 的引用喂给 `MergedTranslation`，并把链头指向归并器：[src/rime/menu.cc:15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L15)。

```cpp
Menu::Menu() : merged_(new MergedTranslation(candidates_)), result_(merged_) {}
```

注意 `MergedTranslation` 的构造参数是 `const CandidateList&`（见 [translation.h:87](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L87) 与 [translation.cc:101-104](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L101-L104)），它把这份引用存为 `previous_candidates_`。从这一刻起，归并器就「连着」`Menu` 的候选区了。

最后看一个容易被忽略、但很重要的注释——`candidate_count()` 的告警：[src/rime/menu.h:38-40](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.h#L38-L40)。

```cpp
  // CAVEAT: returns the number of candidates currently obtained,
  // rather than the total number of available candidates.
  size_t candidate_count() const { return candidates_.size(); }
```

它返回的是「**目前已经拉到的**候选数」，而不是「候选总数」。因为 `Menu` 是惰性的，总有多少候选它自己都不知道（要等 `result_->exhausted()` 才算到头）。这也是 `empty()` 必须同时检查「候选空」和「链耗尽」两个条件的原因：[src/rime/menu.cc:67-69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L67-L69)。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认「共享候选区」这一设计的存在，而不仅听讲解。

**操作步骤**：

1. 打开 [src/rime/menu.cc:15](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L15)，确认 `MergedTranslation(candidates_)` 把 `candidates_` 按引用传入。
2. 跳到 [src/rime/translation.h:99](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L99)，确认 `MergedTranslation` 用 `const CandidateList& previous_candidates_;` 保存这份引用。
3. 打开 [src/rime/menu.cc:22-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L22-L24) 的 `AddFilter`，确认它把 `&candidates_`（指针）传给 `filter->Apply`。
4. 跳到 [src/rime/gear/uniquifier.cc:68-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/uniquifier.cc#L68-L71)，看 `Uniquifier::Apply` 如何把这个指针存进 `UniquifiedTranslation`，并在去重时遍历它。

**需要观察的现象**：`candidates_` 这一份 `vector`，同时被「归并器（引用）」和「每个 Filter（指针）」持有，形成一条共享链。

**预期结果**：你能画出 `candidates_` 被 `Menu`、`MergedTranslation`、`UniquifiedTranslation` 三方共享的关系，并解释「`Prepare` 往 `candidates_` 里 `push_back` 一个候选后，去重 Filter 立刻能看到它」这件事为什么成立——因为它们看的是同一块内存。

> 本实践为源码阅读型，无需运行，结论可直接从代码确认。

#### 4.1.5 小练习与答案

**练习 1**：既然 `candidate_count()` 只返回「已拉到的」数量，那么调用方如何判断「后面真的没有候选了」？

**参考答案**：单独看 `candidate_count()` 不够，必须结合 `result_->exhausted()`。`Menu::empty()` 就是这么做的（[menu.cc:67-69](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L67-L69)）：候选空 **且** 链耗尽，才算真的空。

**练习 2**：`MergedTranslation` 为什么需要 `previous_candidates_` 这份引用？去掉它会怎样？

**参考答案**：它把这份引用透传给 `Translation::Compare(other, candidates)`（见 [translation.h:30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.h#L30)），让选举过程中的比较能感知「已经吐出去的候选」。去掉后，k 路归并就失去了对历史候选的可见性，某些需要去重/重排的子类将无法正确工作。

### 4.2 AddTranslation 与 AddFilter：装配候选流水线

#### 4.2.1 概念说明

`Menu` 对外暴露两个装配入口：`AddTranslation` 把一个 Translator 的输出塞进归并器；`AddFilter` 把一个 Filter 包到当前候选流的最外层。两者共同把「若干翻译器 + 若干过滤器」组装成一条**洋葱式**的候选流：最内层是 `MergedTranslation`（归并所有翻译器），每加一个 Filter 就在洋葱外面再裹一层。

注意一个细节：`AddFilter` **替换** `result_`（`result_ = filter->Apply(result_, &candidates_)`），而不是把 Filter 存进某个列表。也就是说，Filter 链不是「平铺存放、消费时遍历」，而是**在装配期就被层层嵌套成一棵装饰器树**，消费时只需不断对 `result_` 调 `Peek/Next` 即可——所有 Filter 逻辑都编译进了这棵树。

#### 4.2.2 核心流程

```
初始：result_ = merged_            // 内核：归并器

AddTranslation(t):
    merged_ += t                   // 归并器多一路输入
    // result_ 不变，仍是 merged_

AddFilter(f):
    result_ = f->Apply(result_, &candidates_)
    // 旧 result_（比如 merged_）被包进 f 返回的新 Translation 里
    // 此后 Peek/Next 走的是 f 这层
```

若有 translator `T1, T2` 与 filter `F1, F2`，依次 `AddTranslation(T1)`、`AddTranslation(T2)`、`AddFilter(F1)`、`AddFilter(F2)`，最终结构是：

```
result_ = F2( F1( Merged(T1, T2) ) )
```

候选从最内层的 `Merged` 流出，途经 `F1`、`F2` 两道加工，最后到达 `Menu::Prepare`。

#### 4.2.3 源码精读

`AddTranslation` 直接委托给 `MergedTranslation::operator+=`：[src/rime/menu.cc:17-20](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L17-L20)。

```cpp
void Menu::AddTranslation(an<Translation> translation) {
  *merged_ += translation;
  DLOG(INFO) << merged_->size() << " translations added.";
}
```

`operator+=` 会把非空且未耗尽的翻译器收进 `translations_`，并立即调用一次 `Elect()` 重新选举当前最优翻译器：[src/rime/translation.cc:154-160](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L154-L160)。

`AddFilter` 的实现就一行，但信息量很大：[src/rime/menu.cc:22-24](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L22-L24)。

```cpp
void Menu::AddFilter(Filter* filter) {
  result_ = filter->Apply(result_, &candidates_);
}
```

`Filter::Apply` 的签名（[filter.h:28-29](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/filter.h#L28-L29)）清楚地表明：它吃一条 `Translation`、吐一条新的 `Translation`，并额外收一个指向候选区的指针：

```cpp
virtual an<Translation> Apply(an<Translation> translation,
                              CandidateList* candidates) = 0;
```

以 `Uniquifier` 为例，它的 `Apply` 返回一个 `UniquifiedTranslation`，后者持有 `candidates_` 指针，在每次 `Peek` 时都会拿当前候选去和 `candidates_` 里已有的候选比对，命中重复就用 `UniquifiedCandidate` 合并：[src/rime/gear/uniquifier.cc:68-71](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/uniquifier.cc#L68-L71)。

最后看真正的装配现场——`ConcreteEngine::TranslateSegments`：[src/rime/engine.cc:203-233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L203-L233)。关键片段：

```cpp
auto menu = New<Menu>();
for (auto& translator : translators_) {
  auto translation = translator->Query(input, segment);
  if (!translation) continue;
  if (translation->exhausted()) continue;   // "futile translation" 直接丢弃
  menu->AddTranslation(translation);        // <-- 喂给归并器
}
for (auto& filter : filters_) {
  if (filter->AppliesToSegment(&segment)) {
    menu->AddFilter(filter.get());          // <-- 层层包裹
  }
}
segment.menu = menu;
segment.selected_index = 0;
```

注意两个细节：①「已经耗尽的翻译」会被跳过（避免往归并器里塞空流）；②`Filter::AppliesToSegment` 决定该 Filter 是否作用于当前分段（默认全部为真，特殊 Filter 可按 tag 选择性参与）。

#### 4.2.4 代码实践

**实践目标**：在真实的方案配置里找到 Filter 链的来源，把它和代码里的装配过程对应起来。

**操作步骤**：

1. 打开 `data/minimal/luna_pinyin.schema.yaml`，找到 `engine/filters` 列表（通常含 `simplifier`、`uniquifier` 等）。
2. 对照 [engine.cc:224-228](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L224-L228) 的 `for (auto& filter : filters_)` 循环，理解 YAML 里 filters 列表的顺序就是 Filter 包裹的**由内向外**顺序。
3. 写下 `data/minimal/luna_pinyin.schema.yaml` 中 filters 列表对应的洋葱结构，形如 `result_ = 最后一个filter( ... uniquifier( Merged(各 translator ) ) )`。

**需要观察的现象**：filters 在 YAML 里的书写顺序与代码里 `AddFilter` 的调用顺序一致，因此**写在最后的 Filter 包在最外层**，最后才作用于候选。

**预期结果**：你能说出「`uniquifier` 通常写在 filters 列表末尾」的原因——它要在繁简转换等 Filter 之后才做最终去重，看到的是转换后的最终文本。

> 本实践为配置阅读型，结论可直接从 `data/minimal/luna_pinyin.schema.yaml` 与源码对照得出。

#### 4.2.5 小练习与答案

**练习 1**：如果同一个 Filter 在 `engine/filters` 里被列了两次，会发生什么？

**参考答案**：`AddFilter` 会被调用两次，`result_` 会被该 Filter 包两层。语义上等于该过滤逻辑执行两遍（例如 `uniquifier` 包两次，去重逻辑跑两遍，结果通常不变但多余）。代码没有去重保护，完全由配置负责。

**练习 2**：`AddFilter` 接收的是裸指针 `Filter*`（`filter.get()`），而 `AddTranslation` 接收的是 `an<Translation>`（智能指针）。为什么 Filter 不担心生命周期？

**参考答案**：`Filter` 的所有权属于引擎的 `filters_` 容器（见 `Engine` 持有的四组组件），其生命周期不短于 `Menu`。`Apply` 只是把 Filter 的**行为**编进返回的 `Translation` 装饰器树，不持有 Filter 本身，因此传裸指针即可。而 `Translation` 是临时构造的、归并器要长期持有，所以用智能指针管理所有权。

### 4.3 Prepare：按需拉取候选

#### 4.3.1 概念说明

`Prepare(count)` 是 `Menu` 的核心消费方法：它保证 `candidates_` 里**至少有 count 个候选**（或在 `result_` 耗尽前尽可能多）。它体现了「拉模型」——由调用方提出需求，`Menu` 才向 `result_` 链索取候选。整个 librime 里，凡是要「看第 N 个候选」的地方，都会先 `Prepare(N+1)` 把候选拉够。

#### 4.3.2 核心流程

```
Prepare(requested):
    while (candidates_.size() < requested 且 result_->exhausted() == false):
        cand = result_->Peek()       // 经 F2->F1->Merged 一路求值到当前最优候选
        if (cand): candidates_.push_back(cand)
        result_->Next()              // 推进链头游标，准备下一个
    return candidates_.size()
```

两个要点：

1. **`Peek` 可能为空**。某些 Filter 会「吞掉」候选（例如 `charset_filter` 滤掉不合字符集的候选）。被吞掉的候选不会被 `push_back`，但 `Next()` 照常推进，于是循环会继续往后找——这正是「过滤」在惰性求值里的实现方式。
2. **`Next` 一定会调用**。无论 `Peek` 是否拿到候选，都必须推进游标，否则会死循环在同一个被吞掉的候选上。

#### 4.3.3 源码精读

`Prepare` 的实现非常精炼：[src/rime/menu.cc:26-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L26-L35)。

```cpp
size_t Menu::Prepare(size_t requested) {
  DLOG(INFO) << "preparing " << requested << " candidates.";
  while (candidates_.size() < requested && !result_->exhausted()) {
    if (auto cand = result_->Peek()) {
      candidates_.push_back(cand);
    }
    result_->Next();
  }
  return candidates_.size();
}
```

`GetCandidateAt` 是 `Prepare` 的典型调用方之一，它把「要第 index 个候选」翻译成「至少 Prepare(index+1)」：[src/rime/menu.cc:60-65](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L60-L65)。

```cpp
an<Candidate> Menu::GetCandidateAt(size_t index) {
  if (index >= candidates_.size() && index >= Prepare(index + 1)) {
    return nullptr;        // 拉够了仍然没有，说明真的没有
  }
  return candidates_[index];
}
```

整个项目里，`Prepare` 的调用方很多，体现了「拉模型」的普遍性：

- `Context::Highlight(index)` 在高亮前先 `Prepare(index+1)`，见 [context.cc:136](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/context.cc#L136)；
- `speller` 判断「是否唯一候选」时 `Prepare(2)`，见 [gear/speller.cc:168](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/speller.cc#L168)；
- `selector` 翻页时按页边界 `Prepare(page_start + page_size)`，见 [gear/selector.cc:178](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L178)；
- `switcher` 列方案前 `Prepare(2)`，见 [switcher.cc:206](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/switcher.cc#L206)。

#### 4.3.4 代码实践

**实践目标**：观察「过滤型 Filter 吞候选」如何依赖 `Peek` 返回空 + `Next` 推进这一组合。

**操作步骤**：

1. 打开 [menu.cc:28-33](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L28-L33) 的 while 循环，假设在 `if (auto cand = result_->Peek())` 这一行后面**临时加一行日志**（仅用于理解，不提交）：

   ```cpp
   // 示例代码：仅用于理解，勿提交
   if (auto cand = result_->Peek()) {
     candidates_.push_back(cand);
   } else {
     DLOG(INFO) << "a candidate was swallowed by filters.";
   }
   result_->Next();
   ```

2. 阅读任意一个「会吞候选」的 Filter（如 `gear/charset_filter.cc` 或 `gear/single_char_filter.cc`），看它的 `Apply` 返回的 Translation 在不满足条件时如何让 `Peek` 返回空、并靠 `Next` 跳过。

**需要观察的现象**：当一个候选被 Filter 拒绝时，`Prepare` 的循环不会把它加入 `candidates_`，但会继续向后搜索，直到凑够 `requested` 个或链耗尽。

**预期结果**：你能解释「为什么过滤不会造成候选数量虚高、也不会死循环」——空候选不入列，`Next` 照常推进。

> 本实践为「修改局部参数并说明观察现象」的源码阅读型实践。若你已在本地按 u1-l2 编译 librime 并启用 `ENABLE_LOGGING`，可在日志里直接看到上述吞候选事件；否则结论可从代码静态推出，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假设 `result_` 里接下来有 10 个候选，但中间第 3、5 个被某 Filter 吞掉。调用 `Prepare(5)` 后，`candidates_.size()` 是多少？

**参考答案**：是 5。被吞掉的不计入，循环会一直跑到累计 5 个**有效**候选为止（过程中实际从 `result_` 消费了 7 个原始候选）。

**练习 2**：如果把 `result_->Next()` 这一行删掉，会发生什么？

**参考答案**：一旦 `Peek` 拿到一个候选，游标不推进，下次 `Peek` 还是同一个，`push_back` 会无限重复同一个候选，直到 `candidates_.size()` 达到 `requested`——候选列表被同一个候选填满，且永远不会 `exhausted`。这正说明 `Next` 必须无条件调用。

### 4.4 CreatePage：分页与高亮索引

#### 4.4.1 概念说明

`CreatePage(page_size, page_no)` 把已经拉到的候选按 `page_size` 切成等长的「页」，返回第 `page_no` 页。它是 `Menu` 与前端之间的最终接口：前端拿到的 `RimeContext.menu` 就是某一次 `CreatePage` 的产物（见后文 rime_api_impl.h）。

分页的关键不是「切片」本身（那只是 `std::copy`），而是切片前要保证**这一页所需的候选已经拉够**——如果还没拉够，就在切片前先 `Prepare`。同时，`CreatePage` 还要回答两个前端关心的问题：①这一页是不是最后一页（`is_last_page`）；②当前高亮的是这一页里的第几个（由调用方按取模算）。

`page_size` 的来源是方案配置 `menu/page_size`，在 `Schema` 构造时预提取，默认 5：[schema.cc:32-35](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/schema.cc#L32-L35)。

#### 4.4.2 核心流程

设 `page_size = P`、`page_no = k`：

\[
\text{start\_pos} = P \times k,\qquad \text{end\_pos} = \text{start\_pos} + P
\]

判定流程：

```
若 end_pos > candidates_.size():           // 本页右端超出已拉取范围
    若 result_->exhausted():               // 后面真没了
        end_pos = candidates_.size()       // 截到实际末尾
    否则:
        end_pos = Prepare(end_pos)         // 现拉到 end_pos 个
    若 start_pos >= end_pos:
        返回 NULL                          // 这一页是空的（例如翻过头了）
    end_pos = min(start_pos + page_size, end_pos)   // 防止 Prepare 拉过头

构造 Page:
    page->is_last_page = result_->exhausted() 且 (end_pos == candidates_.size())
    拷贝 candidates_[start_pos, end_pos) 进 page->candidates
```

高亮索引由**调用方**计算（不在 `CreatePage` 内部），公式是整除与取模：

\[
\text{page\_no} = \left\lfloor \frac{\text{selected\_index}}{\text{page\_size}} \right\rfloor,\qquad
\text{highlighted\_candidate\_index} = \text{selected\_index} \bmod \text{page\_size}
\]

即「当前选中的候选落在第几页」就是它整除页大小得到的商，「在这一页里排第几」就是余数。

#### 4.4.3 源码精读

`CreatePage` 的完整实现：[src/rime/menu.cc:37-58](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L37-L58)。

```cpp
Page* Menu::CreatePage(size_t page_size, size_t page_no) {
  size_t start_pos = page_size * page_no;
  size_t end_pos = start_pos + page_size;
  if (end_pos > candidates_.size()) {
    if (result_->exhausted())
      end_pos = candidates_.size();
    else
      end_pos = Prepare(end_pos);                       // 现拉
    if (start_pos >= end_pos)
      return NULL;                                      // 空页
    end_pos = (std::min)(start_pos + page_size, end_pos);
  }
  Page* page = new Page;
  if (!page) return NULL;
  page->page_size = page_size;
  page->page_no = page_no;
  page->is_last_page = result_->exhausted() && (end_pos == candidates_.size());
  std::copy(candidates_.begin() + start_pos, candidates_.begin() + end_pos,
            std::back_inserter(page->candidates));
  return page;
}
```

三个细节值得圈出：

1. **`(std::min)` 加括号**：这是因为 Windows 头文件里有 `min` 宏的坑，用括号包住函数名避免被宏替换，是 librime 里常见的写法。
2. **`Prepare` 可能拉过头**：`Prepare(end_pos)` 返回的 `candidates_.size()` 可能大于 `end_pos`（之前已拉过更多），所以再用 `min` 截回本页边界。
3. **`is_last_page` 的双条件**：必须是「链已耗尽」**且**「本页右端正好贴着候选末尾」。如果链没耗尽，即便本页凑不满也不能算最后一页（因为后面可能还有）。

真正的消费者在 C API 实现层：[src/rime_api_impl.h:236-281](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L236-L281)。关键片段：

```cpp
int page_size = 5;
Schema* schema = session->schema();
if (schema) page_size = schema->page_size();
int selected_index = seg.selected_index;
int page_no = selected_index / page_size;                       // 整除定页
the<Page> page(seg.menu->CreatePage(page_size, page_no));
if (page) {
  context->menu.page_size = page_size;
  context->menu.page_no = page_no;
  context->menu.is_last_page = Bool(page->is_last_page);
  context->menu.highlighted_candidate_index = selected_index % page_size;  // 取模定本页位置
  context->menu.num_candidates = page->candidates.size();
  // ... 把 page->candidates 拷成 RimeCandidate[]
}
```

这段代码把 `Menu` 的 `Page` 翻译成 C 层的 `RimeMenu`，正是前端（Squirrel/Weasel 等）最终读到的结构。

至于「翻页」交互，由 `gear/selector.cc` 驱动。例如「下一页」会先按页边界 `Prepare` 拉够，再移动 `selected_index`：[gear/selector.cc:175-180](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/gear/selector.cc#L175-L180)。当翻到末尾且 `page_down_cycle`（方案配置项）为真时，会循环回到第一页——这正是分页与方案配置联动的体现。

#### 4.4.4 代码实践

**实践目标**：用一个具体的 `selected_index` 与 `page_size`，手工演算整除/取模，验证它落到正确的页与高亮位置。

**操作步骤**：

1. 设 `page_size = 5`（默认值），分别取 `selected_index = 0, 3, 7, 12`。
2. 按公式计算 `page_no` 与 `highlighted_candidate_index`：

   | selected_index | page_no | highlighted_candidate_index |
   |---|---|---|
   | 0 | 0 | 0 |
   | 3 | 0 | 3 |
   | 7 | 1 | 2 |
   | 12 | 2 | 2 |

3. 对照 [rime_api_impl.h:243-249](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L243-L249)，确认代码里就是 `page_no = selected_index / page_size` 与 `highlighted_candidate_index = selected_index % page_size`。
4. 思考边界：若 `selected_index = 7` 且 `result_` 实际只有 6 个候选，`CreatePage(5, 1)` 会怎样？——按 [menu.cc:40-48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L40-L48)，`end_pos=10 > size=6`，`Prepare(10)` 后仍只有 6，`start_pos=5 < end_pos=6`，返回只含第 6 个候选的一页，且 `is_last_page=true`。

**需要观察的现象**：高亮索引永远在 `[0, page_size)` 范围内；当候选总数不是 `page_size` 的整数倍时，最后一页会短一些，且 `is_last_page` 为真。

**预期结果**：你能用一张表把任意 `selected_index` 映射到 `(page_no, highlighted)`，并能解释「最后一页不满」时 `CreatePage` 如何安全返回。

> 本实践为演算型，结论可直接由公式与代码验证，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：`is_last_page` 为什么必须是 `result_->exhausted() && end_pos == candidates_.size()` 两个条件的「与」，而不是只看「本页候选数 < page_size」？

**参考答案**：「本页不满」可能只是因为还没来得及 `Prepare` 拉够，并不代表后面真的没了。只有 `result_` 真的耗尽（没有更多候选可拉）**并且**本页右端贴着已拉到的末尾，才能确定是最后一页。只看「不满」会在「还没翻页拉取」时误判。

**练习 2**：`CreatePage` 在什么情况下返回 `NULL`？

**参考答案**：当 `start_pos >= end_pos` 时返回 `NULL`，即这一页范围内一个候选都没有。典型场景是「翻过头」：请求的页号超过实际候选能填满的页数，且后续也没有更多候选可拉（参见 [menu.cc:45-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/menu.cc#L45-L46)）。

## 5. 综合实践

把本讲四个模块串起来，画出**一次完整「输入 → 显示第一页候选」的数据流**。建议在纸上或文本里完成：

**场景**：用户已输入 `ni'hao`，引擎完成分段（单个 abc 段），`translators_` 含 `script_translator`，`filters_` 含 `simplifier` 与 `uniquifier`，`page_size = 5`，前端请求当前 context。

**要求画出以下时序**：

1. **装配阶段**（[engine.cc:203-233](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc#L203-L233)）：
   - `new Menu()` → `result_ = MergedTranslation(candidates_)`；
   - `script_translator->Query(...)` → `AddTranslation` → `merged_ += T`（归并器多一路）；
   - `simplifier->Apply(result_, &candidates_)` → `result_ = simplifier(Merged)`；
   - `uniquifier->Apply(result_, &candidates_)` → `result_ = uniquifier(simplifier(Merged))`；
   - 挂到 `segment.menu`。

2. **消费阶段**（[rime_api_impl.h:244](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L244)）：
   - `page_no = selected_index(0) / 5 = 0`；
   - `CreatePage(5, 0)` → 因 `end_pos=5 > candidates_.size()=0`，调用 `Prepare(5)`；
   - `Prepare(5)` 循环对 `result_` 调 `Peek/Next`：候选从 `Merged` 流出 → 经 `simplifier`（繁简转换）→ 经 `uniquifier`（去重，期间读取共享的 `candidates_`）→ 进入 `candidates_`，直到凑够 5 个或链耗尽；
   - 返回 `Page{page_no=0, is_last_page=?, candidates=[...5 个...]}`；
   - `highlighted_candidate_index = 0 % 5 = 0`。

3. **标注共享候选区**：在图上用一种颜色标出 `candidates_`，并在 `MergedTranslation`、`simplifier` 返回的 Translation、`uniquifier` 返回的 `UniquifiedTranslation` 三处都画一根线指向它，说明它们读的是同一块内存。

**预期产出**：一张包含「装配（洋葱树）」与「消费（拉取循环 + 分页）」两部分的数据流图，并能在图上指出 `candidates_` 的三处共享引用、`Prepare` 的拉取点、`CreatePage` 的分页点与 `is_last_page`/`highlighted` 的计算点。

> 这是源码阅读 + 画图型实践，无需运行代码。完成后，你应当能凭这张图向别人讲清「一次按键后，候选是怎么从词典一路变成屏幕上那一页 5 个候选的」。

## 6. 本讲小结

- `Menu` 是 Translator 与前端之间的**惰性缓冲层**：内部维护一条候选流 `result_` 和一份已确认候选 `candidates_`，做到「显示多少、算多少」。
- **共享候选区**是核心设计：`candidates_` 同时被 `MergedTranslation`（引用）和各 `Filter`（指针）持有，让归并选举与去重都能感知「已吐出的候选」。
- `AddTranslation` 把翻译器塞进 k 路归并器；`AddFilter` 用 `result_ = filter->Apply(result_, &candidates_)` 把当前链头再包一层，Filter 链在**装配期**就被嵌套成一棵装饰器树。
- `Prepare(count)` 是拉模型的心脏：`while` 循环里 `Peek` 取候选（可能被 Filter 吞空）、`Next` 推进游标，直到凑够 `count` 个有效候选或链耗尽。
- `CreatePage(page_size, page_no)` 在切片前按需 `Prepare`，用「链耗尽 且 贴末尾」双条件判定 `is_last_page`；页号与高亮索引由调用方按 `selected_index / page_size`（整除）与 `selected_index % page_size`（取模）计算。

## 7. 下一步学习建议

- **走向流水线全貌**：本讲的 `Menu` 装配发生在 [engine.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/engine.cc) 的 `TranslateSegments` 里，下一篇 [u6-l1 引擎流水线总览](u6-l1-pipeline-overview.md) 会把它和 Processors、Segmentors 串成「一次按键的完整旅程」。
- **深入 Filter 族**：本讲只用了 `uniquifier` 作例子，完整的过滤链（simplifier / charset_filter / single_char_filter / reverse_lookup_filter）在 [u6-l5 Filter 组件族](u6-l5-filters.md) 展开。
- **理解翻页交互**：本讲的 `CreatePage` 是「被动产出页」，主动翻页的交互逻辑在 `gear/selector.cc`，可在阅读 u6-l2 Processor 族时顺带精读其 `PageUp`/`PageDown` 实现。
- **k 路归并的选举算法**：本讲对 `MergedTranslation::Elect` 只点到为止，若想搞清「多翻译器候选如何交叉排序」，可回头精读 [translation.cc:126-152](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/translation.cc#L126-L152)。
